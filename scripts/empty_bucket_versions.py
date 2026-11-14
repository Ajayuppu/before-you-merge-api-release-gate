"""Delete every object version and delete marker in a versioned bucket.

`aws s3 rm --recursive` leaves versions in place, and CloudFormation then
refuses to delete the bucket.
"""

from __future__ import annotations

import sys
from typing import Any


def version_delete_batches(pages: list[dict[str, Any]], batch_size: int = 1000) -> list[list[dict[str, str]]]:
    objects: list[dict[str, str]] = []
    for page in pages:
        for key in ("Versions", "DeleteMarkers"):
            for item in page.get(key) or []:
                objects.append({"Key": item["Key"], "VersionId": item["VersionId"]})
    return [objects[start : start + batch_size] for start in range(0, len(objects), batch_size)]


def purge_bucket(client: Any, bucket: str) -> int:
    """Return how many versions and delete markers were deleted. A missing bucket deletes nothing."""
    deleted = 0
    token: tuple[str, str] | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "MaxKeys": 1000}
        if token:
            kwargs["KeyMarker"] = token[0]
            kwargs["VersionIdMarker"] = token[1]
        try:
            page = client.list_object_versions(**kwargs)
        except Exception as exc:
            code = ""
            response = getattr(exc, "response", None)
            if isinstance(response, dict):
                code = str((response.get("Error") or {}).get("Code") or "")
            if "NoSuchBucket" in code or "NoSuchBucket" in type(exc).__name__ or "NotFound" in code:
                return deleted
            raise
        for batch in version_delete_batches([page]):
            client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
        if not page.get("IsTruncated"):
            return deleted
        token = (page.get("NextKeyMarker") or "", page.get("NextVersionIdMarker") or "")


def existence_line(label: str, present: bool | None) -> str:
    if present is None:
        state = "unknown"
    else:
        state = "exists" if present else "absent"
    return f"{label}: {state}"


def existence_report(rows: list[tuple[str, bool | None]]) -> str:
    return "\n".join(existence_line(label, present) for label, present in rows)


def main() -> int:
    import boto3

    region = sys.argv[1]
    buckets = [item for item in sys.argv[2:] if item]
    client = boto3.client("s3", region_name=region)
    for bucket in buckets:
        removed = purge_bucket(client, bucket)
        print(f"Removed {removed} object versions and delete markers from {bucket}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
