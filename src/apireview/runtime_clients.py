"""Lazy AWS SDK clients for the Lambda runtime.

Importing this module does not contact AWS and does not import boto3.
Clients are created on first use and reused for the life of the process.
"""

from __future__ import annotations

import os
from typing import Any

from apireview.aws_dynamo import DocumentTable
from apireview.errors import FailClosed

_clients: dict[tuple[str, str], Any] = {}


def lambda_runtime() -> bool:
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def region_name() -> str:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise FailClosed("AWS_REGION is not set")
    return region


def load_boto3() -> Any:
    import boto3

    return boto3


def sdk_client(service: str) -> Any:
    region = region_name()
    key = (service, region)
    cached = _clients.get(key)
    if cached is None:
        cached = load_boto3().client(service, region_name=region)
        _clients[key] = cached
    return cached


def clear_clients() -> None:
    _clients.clear()


def document_table(settings: Any) -> DocumentTable:
    return DocumentTable(sdk_client("dynamodb"), settings.table_name)


def blob_store(settings: Any) -> Any:
    from apireview.handlers.lambda_handlers import BlobStore

    return BlobStore(sdk_client("s3"), settings.bucket_name)


def stepfunctions_client() -> Any:
    return sdk_client("stepfunctions")


def events_client() -> Any:
    return sdk_client("events")


def bedrock_runtime_client() -> Any:
    return sdk_client("bedrock-runtime")


def code_interpreter_client() -> Any:
    return sdk_client("bedrock-agentcore")
