"""Argument parsing for the deployed API client. This does not call AWS."""

from __future__ import annotations

from pathlib import Path

from scripts.aws_demo import build_parser, remember_job, resolve_job_id


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


def test_last_job_id_is_saved_and_reused(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    remember_job('{"jobId": "job_63b4574b6378286872e1", "status": "RECEIVED"}')
    assert (tmp_path / ".api-review-last-job").read_text(encoding="utf-8").strip() == "job_63b4574b6378286872e1"
    assert resolve_job_id("last") == "job_63b4574b6378286872e1"
    assert resolve_job_id("job_other") == "job_other"


def test_profile_is_unset_when_omitted_and_aws_profile_is_absent(monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    args = build_parser().parse_args(["--region", "us-east-1", "status", "--job-id", "job_abc"])
    assert args.profile is None
