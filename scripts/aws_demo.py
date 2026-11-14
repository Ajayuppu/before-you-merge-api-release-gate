"""SigV4 client for the deployed review API.

Run this in CloudShell after scripts/deploy_cloudshell.sh. The reference test
suite does not execute it and does not import boto3.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the deployed API review endpoint with SigV4.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--stack-name", default="api-review")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--old", required=True)
    submit.add_argument("--new", required=True)
    submit.add_argument("--fixture", required=True)
    submit.add_argument("--mock-version", required=True)
    submit.add_argument("--idempotency-key", required=True)
    status = sub.add_parser("status")
    status.add_argument("--job-id", required=True)
    report = sub.add_parser("report")
    report.add_argument("--job-id", required=True)
    context = sub.add_parser("review-context")
    context.add_argument("--job-id", required=True)
    review = sub.add_parser("review")
    review.add_argument("--job-id", required=True)
    review.add_argument("--evidence-hash", required=True)
    review.add_argument("--nonce", required=True)
    review.add_argument("--decision", choices=["approve", "reject"], required=True)
    review.add_argument("--comment", default="")
    args = parser.parse_args()
    endpoint = stack_output(args.region, args.stack_name, "ReviewApiEndpoint")
    if args.command == "submit":
        body = {
            "fixture": json.loads(Path(args.fixture).read_text()),
            "mockVersion": args.mock_version,
            "newSpec": json.loads(Path(args.new).read_text()),
            "oldSpec": json.loads(Path(args.old).read_text()),
        }
        return call(endpoint + "/jobs", "POST", args.region, body, {"Idempotency-Key": args.idempotency_key})
    if args.command == "status":
        return call(endpoint + "/jobs/" + args.job_id, "GET", args.region, None, {})
    if args.command == "report":
        return call(endpoint + "/jobs/" + args.job_id + "/report", "GET", args.region, None, {})
    if args.command == "review-context":
        return call(endpoint + "/jobs/" + args.job_id + "/review-context", "GET", args.region, None, {})
    if args.command == "review":
        return call(
            endpoint + "/reviews",
            "POST",
            args.region,
            {
                "comment": args.comment,
                "decision": args.decision,
                "evidenceHash": args.evidence_hash,
                "jobId": args.job_id,
                "nonce": args.nonce,
            },
            {},
        )
    return 2


def stack_output(region: str, stack_name: str, key: str) -> str:
    import boto3

    client = boto3.client("cloudformation", region_name=region)
    stacks = client.describe_stacks(StackName=stack_name)["Stacks"]
    for row in stacks[0].get("Outputs", []):
        if row["OutputKey"] == key:
            return row["OutputValue"].rstrip("/")
    raise SystemExit(f"stack output {key} was not found")


def call(url: str, method: str, region: str, body: dict | None, headers: dict[str, str]) -> int:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = AWSRequest(method=method, url=url, data=data, headers={"content-type": "application/json", **headers})
    credentials = Session().get_credentials()
    if credentials is None:
        raise SystemExit("no AWS credentials in this environment")
    SigV4Auth(credentials.get_frozen_credentials(), "execute-api", region).add_auth(request)
    outbound = urllib.request.Request(url, data=data, method=method, headers=dict(request.headers.items()))
    try:
        with urllib.request.urlopen(outbound, timeout=30) as response:
            print(response.read().decode("utf-8"))
            return 0
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
