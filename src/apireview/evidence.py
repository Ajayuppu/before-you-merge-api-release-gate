"""Evidence sealing. The hash covers the job, artifacts, harness, and results."""

from __future__ import annotations

from typing import Any

from apireview.canonical import canonical_bytes, canonical_dumps, sha256_bytes, sha256_text
from apireview.diff import breaking_findings

EVIDENCE_VERSION = "1.0.0"


def seal_evidence(
    *,
    job_id: str,
    old_spec_hash: str,
    new_spec_hash: str,
    fixture_hash: str,
    mock_version: str,
    findings: list[dict[str, Any]],
    harness: dict[str, Any] | None,
    checker: dict[str, Any] | None,
    advisory: dict[str, Any],
    agent_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verdict = authoritative_verdict(findings, harness)
    bundle = {
        "evidenceVersion": EVIDENCE_VERSION,
        "jobId": job_id,
        "oldSpecHash": old_spec_hash,
        "newSpecHash": new_spec_hash,
        "fixtureHash": fixture_hash,
        "mockVersion": mock_version,
        "findings": findings,
        "harness": harness,
        "checker": {
            "authoritative": False,
            "checkerVersion": None if checker is None else checker.get("checkerVersion"),
            "checkerSourceHash": None if checker is None else checker.get("checkerSourceHash"),
            "mode": None if checker is None else checker.get("mode"),
            "ok": None if checker is None else checker.get("ok"),
            "findings": [] if checker is None else checker.get("findings", []),
        },
        "advisory": {
            "authoritative": False,
            "includedInVerdict": False,
            "deterministicHash": advisory.get("deterministicHash"),
            "modelAdvisoryHash": advisory.get("modelAdvisoryHash"),
        },
        "agentTrace": {
            "authoritative": False,
            "tools": [] if agent_trace is None else list(agent_trace.get("tools", [])),
            "called": [] if agent_trace is None else [item.get("name") for item in agent_trace.get("results", [])],
            "denied": [] if agent_trace is None else list(agent_trace.get("denied", [])),
        },
        "verdict": verdict,
    }
    digest = sha256_bytes(canonical_bytes(bundle))
    bundle["evidenceHash"] = digest
    return bundle


def authoritative_verdict(findings: list[dict[str, Any]], harness: dict[str, Any] | None) -> str:
    """AI output is intentionally not a parameter."""
    if not harness or not harness.get("complete") or not harness.get("passed"):
        return "BLOCKED"
    if not harness.get("cases"):
        return "BLOCKED"
    if breaking_findings(findings):
        return "BLOCKED"
    return "PASS"


def advisory_content_hash(advisory: dict[str, Any]) -> str:
    visible = {
        "explanations": advisory.get("explanations", []),
        "proposals": advisory.get("proposals", []),
        "source": advisory.get("source"),
    }
    return sha256_text(canonical_dumps(visible))
