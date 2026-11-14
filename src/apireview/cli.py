"""Command line for the local simulation.

Commands: submit, status, report, review, demo, serve.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apireview import LOCAL_SIMULATION_BANNER, __version__
from apireview.errors import ReviewError
from apireview.identity import local_principal
from apireview.ids import ManualClock
from apireview.service import ReviewService
from apireview.snapshot import load_service, save_service

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE = ROOT / ".apireview" / "state.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="apireview", description="Local API breaking-change reviewer.")
    parser.add_argument("--state", default=str(DEFAULT_STATE), help="Path to local simulation state.")
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="Submit old and new OpenAPI documents plus a fixture.")
    _add_principal(submit, default_role="developer")
    submit.add_argument("--old", required=True)
    submit.add_argument("--new", required=True)
    submit.add_argument("--fixture", required=True)
    submit.add_argument("--mock-version", required=True)
    submit.add_argument("--idempotency-key", required=True)

    status = sub.add_parser("status", help="Poll a job.")
    _add_principal(status, default_role="developer")
    status.add_argument("--job-id", required=True)

    report = sub.add_parser("report", help="Fetch a sealed report.")
    _add_principal(report, default_role="developer")
    report.add_argument("--job-id", required=True)

    review = sub.add_parser("review", help="Approve or reject sealed evidence.")
    _add_principal(review, default_role="reviewer")
    review.add_argument("--job-id", required=True)
    review.add_argument("--evidence-hash", required=True)
    review.add_argument("--nonce", required=True)
    review.add_argument("--decision", required=True, choices=["approve", "reject"])
    review.add_argument("--comment", default="")

    sub.add_parser("demo", help="Run the healthy, broken, and corrected orders journeys.")
    serve = sub.add_parser("serve", help="Serve the local simulation HTTP API.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=47831)

    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            print(run_demo())
            return 0
        if args.command == "serve":
            from apireview.http_api import serve as serve_http

            state = Path(args.state)
            service = load_service(state)

            def persist(current: ReviewService) -> None:
                save_service(state, current)

            serve_http(service, args.host, args.port, on_change=persist)
            return 0
        service = load_service(Path(args.state))
        principal = local_principal(args.principal, [args.role])
        if args.command == "submit":
            view = service.submit(
                principal,
                idempotency_key=args.idempotency_key,
                old_spec=Path(args.old).read_bytes(),
                new_spec=Path(args.new).read_bytes(),
                fixture=Path(args.fixture).read_bytes(),
                mock_version=args.mock_version,
                drain=True,
            )
        elif args.command == "status":
            job = service.store.get(f"job#{args.job_id}")
            if job is None:
                print(f"job not found: {args.job_id}", file=sys.stderr)
                return 2
            view = service.public_job(job, principal)
        elif args.command == "report":
            view = service.report(principal, args.job_id)
        elif args.command == "review":
            view = service.review(
                principal,
                job_id=args.job_id,
                evidence_hash=args.evidence_hash,
                nonce=args.nonce,
                decision=args.decision,
                comment=args.comment,
            )
        else:
            return 2
        save_service(Path(args.state), service)
        print(json.dumps(view, indent=2, sort_keys=True))
        if view.get("status") in {"BLOCKED", "REJECTED", "FAILED", "EXPIRED"}:
            return 1
        return 0
    except ReviewError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1


def _add_principal(parser: argparse.ArgumentParser, *, default_role: str) -> None:
    parser.add_argument("--principal", required=True, help="Local simulation principal name, such as ada or ben.")
    parser.add_argument("--role", default=default_role, choices=["developer", "reviewer"])


def run_demo() -> str:
    """Run three orders journeys on a ManualClock and return the text report."""
    clock = ManualClock()
    service = ReviewService(clock=clock)
    ada = local_principal("ada", ["developer"])
    ben = local_principal("ben", ["reviewer"])
    old = (ROOT / "fixtures" / "orders-v1.openapi.json").read_bytes()
    broken = (ROOT / "fixtures" / "orders-v2-broken.openapi.json").read_bytes()
    corrected = (ROOT / "fixtures" / "orders-v2-corrected.openapi.json").read_bytes()
    fixture = (ROOT / "fixtures" / "orders-client.fixture.json").read_bytes()
    lines = [
        f"apireview {__version__} local demo",
        LOCAL_SIMULATION_BANNER,
        "",
    ]
    healthy = _journey(
        service,
        ada,
        ben,
        name="healthy",
        old_spec=old,
        new_spec=old,
        fixture=fixture,
        mock_version="orders-v1",
        idempotency_key="demo-healthy-001",
        decision="approve",
    )
    broken_view = _journey(
        service,
        ada,
        ben,
        name="broken",
        old_spec=old,
        new_spec=broken,
        fixture=fixture,
        mock_version="orders-v2-broken",
        idempotency_key="demo-broken-001",
        decision="approve",
    )
    corrected_view = _journey(
        service,
        ada,
        ben,
        name="corrected",
        old_spec=old,
        new_spec=corrected,
        fixture=fixture,
        mock_version="orders-v2-corrected",
        idempotency_key="demo-corrected-001",
        decision="approve",
    )
    lines.extend(healthy)
    lines.extend(broken_view)
    lines.extend(corrected_view)
    lines.append("executions started: " + str(service.executions.start_count))
    return "\n".join(lines)


def _journey(
    service: ReviewService,
    submitter: Any,
    reviewer: Any,
    *,
    name: str,
    old_spec: bytes,
    new_spec: bytes,
    fixture: bytes,
    mock_version: str,
    idempotency_key: str,
    decision: str,
) -> list[str]:
    view = service.submit(
        submitter,
        idempotency_key=idempotency_key,
        old_spec=old_spec,
        new_spec=new_spec,
        fixture=fixture,
        mock_version=mock_version,
        drain=True,
    )
    report = service.report(submitter, view["jobId"])
    lines = [
        f"== {name} ==",
        f"status: {view['status']}",
        f"verdict: {view.get('authoritativeVerdict')}",
        f"harnessPassed: {view.get('harnessPassed')}",
        f"breakingFindingCount: {view.get('breakingFindingCount')}",
        f"evidenceHash: {view.get('evidenceHash')}",
    ]
    for finding in report["evidence"]["findings"]:
        if finding["severity"] == "breaking":
            lines.append(f"breaking {finding['rule']}: {finding['message']}")
    harness = report["evidence"].get("harness") or {}
    for case in harness.get("cases") or []:
        for assertion in case.get("assertions") or []:
            if not assertion.get("passed"):
                lines.append(f"assertion failed {assertion['name']}: {assertion['message']}")
    checker = report["evidence"].get("checker") or {}
    lines.append(f"checker.authoritative: {checker.get('authoritative')}")
    lines.append(f"checker.ok: {checker.get('ok')}")
    lines.append(f"checker.mode: {checker.get('mode')}")
    lines.append(f"advisory.includedInVerdict: {report['evidence']['advisory']['includedInVerdict']}")
    if view["status"] == "WAITING_REVIEW":
        reviewer_view = service.public_job(service.store.get(f"job#{view['jobId']}"), reviewer)
        try:
            reviewed = service.review(
                reviewer,
                job_id=view["jobId"],
                evidence_hash=view["evidenceHash"],
                nonce=reviewer_view["reviewNonce"],
                decision=decision,
                comment=f"{name} evidence matches the sealed bundle",
            )
            lines.append(f"review: {reviewed['status']}")
        except ReviewError as exc:
            lines.append(f"review rejected: {exc.code}: {exc}")
    else:
        try:
            service.review(
                reviewer,
                job_id=view["jobId"],
                evidence_hash=view.get("evidenceHash") or "missing",
                nonce="0" * 32,
                decision=decision,
                comment="should fail closed",
            )
            lines.append("review unexpectedly succeeded")
        except ReviewError as exc:
            lines.append(f"review rejected: {exc.code}: {exc}")
    lines.append("")
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
