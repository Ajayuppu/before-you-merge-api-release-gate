"""Argument parsing for the deployed API client. This does not call AWS."""

from __future__ import annotations

from scripts.aws_demo import build_parser


def test_profile_flag_is_used_for_the_documented_command(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    args = build_parser().parse_args(
        ["--region", "us-east-1", "--profile", "api-review-developer", "submit", "--old", "a", "--new", "b", "--fixture", "c", "--mock-version", "orders-v1", "--idempotency-key", "k"]
    )
    assert args.profile == "api-review-developer"
    assert args.region == "us-east-1"
    assert args.command == "submit"


def test_profile_defaults_to_aws_profile(monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "from-env")
    args = build_parser().parse_args(["--region", "us-east-1", "status", "--job-id", "job_abc"])
    assert args.profile == "from-env"


def test_profile_is_unset_when_omitted_and_aws_profile_is_absent(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    args = build_parser().parse_args(["--region", "us-east-1", "status", "--job-id", "job_abc"])
    assert args.profile is None
