"""SigV4 client for the deployed review API.

Run this in CloudShell after scripts/deploy_cloudshell.sh. The reference test
suite does not execute it and does not import boto3.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call the deployed API review endpoint with SigV4.")
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", default=os.environ.get("AWS_PROFILE") or None)
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
    return parser


def main() -> int:
    args = build_parser().parse_args()
    endpoint = stack_output(args.region, args.stack_name, "ReviewApiEndpoint", args.profile)
    if args.command == "submit":
        body = {
            "fixture": json.loads(Path(args.fixture).read_text()),
            "mockVersion": args.mock_version,
            "newSpec": json.loads(Path(args.new).read_text()),
            "oldSpec": json.loads(Path(args.old).read_text()),
        }
        return call(endpoint + "/jobs", "POST", args.region, body, {"Idempotency-Key": args.idempotency_key}, args.profile)
    if args.command == "status":
        return call(endpoint + "/jobs/" + args.job_id, "GET", args.region, None, {}, args.profile)
    if args.command == "report":
        return call(endpoint + "/jobs/" + args.job_id + "/report", "GET", args.region, None, {}, args.profile)
    if args.command == "review-context":
        return call(endpoint + "/jobs/" + args.job_id + "/review-context", "GET", args.region, None, {}, args.profile)
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
            args.profile,
        )
    return 2


def stack_output(region: str, stack_name: str, key: str, profile: str | None = None) -> str:
    import boto3
    from botocore.exceptions import ClientError

    sessions = [boto3.Session(profile_name=profile)] if profile else []
    sessions.append(boto3.Session())
    last_denied = False
    for index, session in enumerate(sessions):
        try:
            client = session.client("cloudformation", region_name=region)
            stacks = client.describe_stacks(StackName=stack_name)["Stacks"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if profile and index == 0 and code in {"AccessDenied", "AccessDeniedException"}:
                last_denied = True
                continue
            raise
        if last_denied:
            print("profile cannot read stack outputs; the lookup used ambient credentials", file=sys.stderr)
        for row in stacks[0].get("Outputs", []):
            if row["OutputKey"] == key:
                return row["OutputValue"].rstrip("/")
        raise SystemExit(f"stack output {key} was not found")
    raise SystemExit(f"stack output {key} was not found")


def call(url: str, method: str, region: str, body: dict | None, headers: dict[str, str], profile: str | None = None) -> int:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    data = None if body is None else json.dumps(body).encode("utf-8")
    request = AWSRequest(method=method, url=url, data=data, headers={"content-type": "application/json", **headers})
    session = Session(profile=profile) if profile else Session()
    credentials = session.get_credentials()
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
