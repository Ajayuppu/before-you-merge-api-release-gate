"""Offline checks for the deployment template. CloudFormation was not deployed."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.assemble_template import PLACEHOLDERS, build_template

ROOT = Path(__file__).resolve().parents[1]


def fake_sub(text: str, mapping: dict[str, str]) -> str:
    text = text.replace("$$", "\0")
    for key, value in mapping.items():
        text = text.replace("${" + key + "}", value)
    if "${" in text:
        raise AssertionError(text[text.index("${") : text.index("${") + 80])
    return text.replace("\0", "$")


def test_template_contracts():
    template = build_template()
    resources = template["Resources"]
    assert resources["ReviewStateMachine"]["Properties"]["StateMachineType"] == "STANDARD"
    assert resources["WorkQueue"]["Properties"]["VisibilityTimeout"] == 182
    assert resources["DispatcherMapping"]["Properties"]["MaximumBatchingWindowInSeconds"] == 2
    assert resources["DispatcherMapping"]["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert resources["PublisherMapping"]["Properties"]["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
    assert resources["DispatcherFunction"]["Properties"]["Timeout"] == 30
    target = resources["SubmitRule"]["Properties"]["Targets"][0]
    assert "WorkQueue" in json.dumps(target["Arn"])
    assert "StateMachine" not in json.dumps(target["Arn"])
    assert "DeadLetterConfig" in target
    assert resources["WorkerDlq"]["Type"] == "AWS::SQS::Queue"
    assert resources["EventBridgeDlq"]["Type"] == "AWS::SQS::Queue"
    encoded = json.dumps(template)
    assert "aws_xray_sdk" not in encoded
    assert not re.search(r"\b\d{12}\b", encoded)
    assert "AKIA" not in encoded
    for word in ("price", "pricing", "budget"):
        assert word not in encoded.lower()
    definition = resources["ReviewStateMachine"]["Properties"]["DefinitionString"]["Fn::Sub"][0]
    rendered = fake_sub(definition, {name: f"arn:aws:lambda:region:example:function:{name}" for name in PLACEHOLDERS})
    states = json.loads(rendered)
    assert states["States"]["WaitForReview"]["Resource"].endswith("waitForTaskToken")
    assert states["States"]["WaitForReview"]["Parameters"]["Payload"]["taskToken.$"] == "$$.Task.Token"
    assert states["States"]["WaitForReview"]["TimeoutSeconds"] == 3600
    assert states["States"]["Gate"]["Default"] == "MarkBlocked"
    assert "arn:aws:states:::states:startExecution" not in rendered
    assert resources["HarnessFunction"]["Properties"]["Environment"]["Variables"]["HARNESS_TRANSPORT"] == "signed-mock"
    assert resources["AnalyzeFunction"]["Properties"]["Environment"]["Variables"]["BEDROCK_ADVISORY"] == "disabled"
    assert resources["SpecCheckFunction"]["Properties"]["Environment"]["Variables"]["SPEC_CHECK_MODE"] == "in-process"
    assert resources["SubmitFunction"]["Properties"]["Environment"]["Variables"]["AWS_LAMBDA_EXEC_WRAPPER"] == "/opt/otel-instrument"
    mock_paths = resources["MockApi"]["Properties"]["Body"]["paths"]
    assert "/orders-v1/orders" in mock_paths
    assert "19.95" in mock_paths["/orders-v1/orders"]["get"]["x-amazon-apigateway-integration"]["responses"]["default"]["responseTemplates"]["application/json"]
    broken = mock_paths["/orders-v2-broken/orders"]["get"]["x-amazon-apigateway-integration"]["responses"]["default"]["responseTemplates"]["application/json"]
    assert "next_cursor" not in broken
