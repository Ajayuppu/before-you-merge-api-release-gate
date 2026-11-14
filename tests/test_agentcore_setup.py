"""AgentCore setup helpers. These tests do not call AWS."""

from __future__ import annotations

import json

import re

from scripts.agentcore_setup import (
    APPROVE_TOOL,
    NAME_PATTERNS,
    READ_TOOL,
    SERVICE_PRINCIPAL,
    classify_tool_result,
    gateway_permissions,
    tool_configuration,
    trust_policy,
)


def test_resource_names_match_documented_patterns():
    assert set(NAME_PATTERNS) == {
        "code_interpreter",
        "gateway",
        "gateway_target",
        "policy_engine",
        "policy_read_artifacts",
        "policy_request_spec_check",
        "policy_read_test_results",
        "session",
    }
    for label, (name, pattern) in NAME_PATTERNS.items():
        assert re.fullmatch(pattern, name), label
        assert "-" not in name or label in {"gateway", "gateway_target"}


def test_trust_uses_the_agentcore_service_principal():
    document = trust_policy("111122223333", "us-east-1", "arn:aws:bedrock-agentcore:us-east-1:111122223333:*")
    statement = document["Statement"][0]
    assert statement["Principal"] == {"Service": SERVICE_PRINCIPAL}
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == "111122223333"
    assert statement["Condition"]["ArnLike"]["aws:SourceArn"].startswith("arn:aws:bedrock-agentcore:us-east-1:")


def test_gateway_role_can_call_developer_routes_and_not_reviews():
    document = gateway_permissions("111122223333", "us-east-1", "abc123")
    encoded = json.dumps(document)
    assert "bedrock-agentcore:GetPolicyEngine" in encoded
    assert "bedrock-agentcore:AuthorizeAction" in encoded
    assert "bedrock-agentcore:PartiallyAuthorizeActions" in encoded
    assert "execute-api:Invoke" in encoded
    assert "/live/POST/reviews" in encoded
    assert "bedrock:*" not in encoded
    names = [item["name"] for item in tool_configuration()["toolOverrides"]]
    assert "approve" in names
    assert READ_TOOL.endswith("read_artifacts")
    assert APPROVE_TOOL.endswith("approve")


def test_validation_lines_distinguish_allow_and_deny():
    allowed, _ = classify_tool_result(True, 200, '{"result":{"content":[{"text":"{\\"status\\":\\"BLOCKED\\"}"}]}}')
    denied, _ = classify_tool_result(False, 403, "AccessDeniedException: explicit deny by policy")
    missed, detail = classify_tool_result(False, 200, '{"result":{"status":"sent"}}')
    assert allowed is True
    assert denied is True
    assert missed is False
    assert "sent" in detail
