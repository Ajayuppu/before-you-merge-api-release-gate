"""Versioned bucket cleanup. These tests do not call AWS."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.empty_bucket_versions import existence_report, purge_bucket, version_delete_batches


class _Missing(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchBucket"}}


class _Client:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = list(pages)
        self.deleted: list[dict] = []

    def list_object_versions(self, **kwargs):
        if not self.pages:
            raise _Missing()
        return self.pages.pop(0)

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(Delete["Objects"])


def test_versions_and_delete_markers_batch_at_1000():
    versions = [{"Key": f"k{i}", "VersionId": f"v{i}"} for i in range(1001)]
    markers = [{"Key": "gone", "VersionId": "m1"}]
    batches = version_delete_batches([{"Versions": versions, "DeleteMarkers": markers}])
    assert [len(batch) for batch in batches] == [1000, 2]
    assert batches[-1][-1] == {"Key": "gone", "VersionId": "m1"}


def test_purge_follows_pagination_and_ignores_a_missing_bucket():
    page = {
        "Versions": [{"Key": "a", "VersionId": "1"}],
        "DeleteMarkers": [{"Key": "a", "VersionId": "2"}],
        "IsTruncated": True,
        "NextKeyMarker": "a",
        "NextVersionIdMarker": "2",
    }
    follow = {"Versions": [{"Key": "b", "VersionId": "3"}], "IsTruncated": False}
    client = _Client([page, follow])
    assert purge_bucket(client, "bucket") == 3
    assert client.deleted == [
        {"Key": "a", "VersionId": "1"},
        {"Key": "a", "VersionId": "2"},
        {"Key": "b", "VersionId": "3"},
    ]
    assert purge_bucket(_Client([]), "missing") == 0


def test_existence_report_names_each_resource():
    text = existence_report(
        [
            ("stack api-review", False),
            ("artifact bucket artifacts", True),
            ("table api-review-jobs", None),
        ]
    )
    assert "stack api-review: absent" in text
    assert "artifact bucket artifacts: exists" in text
    assert "table api-review-jobs: unknown" in text


def test_agentcore_examples_use_the_live_names():
    root = Path(__file__).resolve().parents[1] / "cloud" / "agentcore"
    interpreter = json.loads((root / "create_code_interpreter.json").read_text())
    gateway = json.loads((root / "create_gateway.json").read_text())
    policy = json.loads((root / "create_policy.read_artifacts.json").read_text())
    assert interpreter["name"] == "ApiReviewChecker"
    assert gateway["name"] == "ApiReviewTools"
    assert policy["name"] == "AllowReadArtifacts"
    statement = policy["definition"]["cedar"]["statement"]
    assert 'AgentCore::Action::"ReviewTools___read_artifacts"' in statement
    assert "principal.id like" in statement
    assert "principal is AgentCore::IamEntity" in statement
