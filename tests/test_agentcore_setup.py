"""AgentCore setup helpers. These tests do not call AWS."""

from __future__ import annotations

import json

import re

from scripts.agentcore_setup import (
    APPROVE_TOOL,
    MCP_PROTOCOL_VERSION,
    NAME_PATTERNS,
    READ_TOOL,
    SERVICE_PRINCIPAL,
    classify_tool_result,
    code_interpreter_source_arn,
    gateway_permissions,
    negotiated_protocol,
    protocol_from_gateway,
    select_gateway_tools,
    tool_configuration,
    tool_names_from_list,
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


def test_code_interpreter_trust_uses_the_documented_source_arn():
    source = code_interpreter_source_arn("111122223333", "us-east-1")
    assert source == "arn:aws:bedrock-agentcore:us-east-1:111122223333:*"
    assert "code-interpreter/" not in source


def test_mcp_protocol_negotiates_from_the_server_error():
    assert MCP_PROTOCOL_VERSION == "2025-03-26"
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": -32600,
                "message": "Unsupported protocol version: 2025-11-25",
                "data": {"requested": "2025-11-25", "supported": ["2025-03-26"]},
            },
        }
    )
    assert negotiated_protocol(body, "2025-11-25") == "2025-03-26"
    assert negotiated_protocol(body, "2025-03-26") is None
    assert protocol_from_gateway({"protocolConfiguration": {"mcp": {"supportedVersions": ["2025-03-26"]}}}) == "2025-03-26"
    assert protocol_from_gateway({}) == "2025-03-26"


def test_tool_names_come_from_tools_list():
    triple = json.dumps({"result": {"tools": [{"name": "ReviewTools___read_artifacts"}, {"name": "ReviewTools___request_spec_check"}]}})
    double = json.dumps({"result": {"tools": [{"name": "ReviewTools__read_artifacts"}]}})
    assert tool_names_from_list(triple) == ["ReviewTools___read_artifacts", "ReviewTools___request_spec_check"]
    assert select_gateway_tools(tool_names_from_list(triple)) == ("ReviewTools___read_artifacts", "ReviewTools___approve")
    assert select_gateway_tools(tool_names_from_list(double)) == ("ReviewTools__read_artifacts", "ReviewTools__approve")


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
    denied, _ = classify_tool_result(
        False,
        200,
        '{"result":{"isError":true,"content":[{"text":"AuthorizeActionException - Tool Execution Denied: Tool call not allowed due to policy enforcement [No policy applies to the request (denied by default).]"}]}}',
    )
    protocol = '{"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"Unsupported protocol version: 2025-11-25","data":{"supported":["2025-03-26"]}}}'
    protocol_allow, _ = classify_tool_result(True, 400, protocol)
    protocol_deny, _ = classify_tool_result(False, 400, protocol)
    other_error, detail = classify_tool_result(False, 403, "ValidationException: bad arguments")
    missed, missed_detail = classify_tool_result(False, 200, '{"result":{"isError":false,"content":[{"text":"sent"}]}}')
    assert allowed is True
    assert denied is True
    assert protocol_allow is False
    assert protocol_deny is False
    assert other_error is False
    assert "ValidationException" in detail
    assert missed is False
    assert "sent" in missed_detail
