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
    _stream_text,
    checker_result_ok,
    classify_tool_result,
    code_interpreter_source_arn,
    find_gateway_summary,
    gateway_permissions,
    gateway_source_arn,
    negotiated_protocol,
    parse_checker_output,
    CALLER_ROLE,
    POLICY_VALIDATION_MODE,
    action_id,
    format_policy,
    iam_entity_id,
    permitted_action_ids,
    permit_statement,
    policy_engine_attached,
    policy_statement_text,
    policy_status_reasons,
    protocol_from_gateway,
    select_gateway_tools,
    target_tool_names,
    target_config_matches,
    target_configuration,
    tool_configuration,
    tool_names_from_list,
    tools_or_allowed_scope,
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


def test_gateway_trust_stays_account_wide():
    source = gateway_source_arn("111122223333", "us-east-1")
    assert source == "arn:aws:bedrock-agentcore:us-east-1:111122223333:*"
    assert ":gateway/" not in source
    document = trust_policy("111122223333", "us-east-1", source)
    assert document["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] == source
    assert document["Statement"][0]["Principal"] == {"Service": SERVICE_PRINCIPAL}


def test_gateway_lookup_uses_items_and_the_lowercase_id():
    page = {"items": [{"gatewayId": "apireviewtools-l0jmrlboyy", "name": "ApiReviewTools"}]}
    assert find_gateway_summary([page], "ApiReviewTools")["gatewayId"] == "apireviewtools-l0jmrlboyy"
    folded = {"items": [{"gatewayId": "apireviewtools-l0jmrlboyy", "name": "apireviewtools"}]}
    assert find_gateway_summary([folded], "ApiReviewTools")["gatewayId"] == "apireviewtools-l0jmrlboyy"
    assert find_gateway_summary([{"gateways": [], "gatewaySummaries": []}], "ApiReviewTools") is None
    attached = {"policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"}}
    assert policy_engine_attached(attached, "arn:engine") is True
    assert policy_engine_attached({"policyEngineConfiguration": {"arn": "arn:other", "mode": "ENFORCE"}}, "arn:engine") is False


def test_empty_tool_list_uses_the_cedar_scope():
    names, fallback = tools_or_allowed_scope([])
    assert fallback is True
    read_name, approve_name = select_gateway_tools(names)
    assert read_name == "ReviewTools___read_artifacts"
    assert approve_name == "ReviewTools___approve"
    listed, listed_fallback = tools_or_allowed_scope(["ReviewTools__read_artifacts"])
    assert listed_fallback is False
    assert select_gateway_tools(listed) == ("ReviewTools__read_artifacts", "ReviewTools__approve")


def test_checker_output_parses_the_first_object_and_expected_findings():
    raw = (
        "stdout\n"
        '{"authoritative":false,"checkerVersion":"1.0.0","findings":['
        '{"message":"Order.amount must stay type number for old clients.","rule":"checker.amount.type"},'
        '{"message":"OrderPage.next_cursor must remain a required response property.","rule":"checker.next_cursor.required"}'
        '],"ok":false}\n'
    )
    parsed = parse_checker_output(raw)
    assert parsed is not None
    assert checker_result_ok(parsed) is True
    joined = _stream_text(
        {
            "stream": [
                {"result": {"content": [{"type": "text", "text": raw[:80]}]}},
                {"result": {"stdout": raw[80:]}},
            ]
        }
    )
    assert checker_result_ok(parse_checker_output(joined)) is True
    assert checker_result_ok(parse_checker_output(raw[:120])) is False


def test_iam_permits_use_the_target_tool_names_and_report_failures():
    arn = "arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/apireviewtools-l0jmrlboyy"
    target = {
        "name": "ReviewTools",
        "targetConfiguration": {
            "mcp": {
                "apiGateway": {
                    "apiGatewayToolConfiguration": {
                        "toolFilters": [{"filterPath": "/jobs/{jobId}", "methods": ["GET"]}],
                        "toolOverrides": [
                            {"name": "read_artifacts", "path": "/jobs/{jobId}", "method": "GET"},
                            {"name": "request_spec_check", "path": "/jobs/{jobId}/report", "method": "GET"},
                            {"name": "read_test_results", "path": "/jobs", "method": "POST"},
                            {"name": "approve", "path": "/reviews", "method": "POST"},
                        ],
                    }
                }
            }
        },
    }
    assert target_tool_names(target) == ["read_artifacts", "request_spec_check", "read_test_results", "approve"]
    actions = permitted_action_ids("ReviewTools", target_tool_names(target))
    assert actions[0] == ("AllowReadArtifacts", "ReviewTools" + "___" + "read_artifacts")
    assert all("approve" not in action for _name, action in actions)
    assert action_id("ReviewTools", "ReviewTools__read_artifacts") == "ReviewTools__read_artifacts"
    statement = permit_statement(arn, actions[0][1], iam_entity_id("111122223333", CALLER_ROLE))
    assert "principal is AgentCore::IamEntity" in statement
    assert 'principal.id like "arn:aws:sts::111122223333:assumed-role/ApiReviewDeveloper*"' in statement
    assert f'resource == AgentCore::Gateway::"{arn}"' in statement
    assert 'AgentCore::Action::"ReviewTools___read_artifacts"' in statement
    assert POLICY_VALIDATION_MODE == "FAIL_ON_ANY_FINDINGS"
    failed = {
        "name": "AllowReadArtifacts",
        "status": "CREATE_FAILED",
        "enforcementMode": "ACTIVE",
        "statusReasons": ["Validation failed: Action 'read_artifacts' is not defined in the schema for any associated gateway"],
        "definition": {"cedar": {"statement": "permit (principal, action, resource);"}},
    }
    assert policy_status_reasons(failed)[0].startswith("Validation failed")
    assert "CREATE_FAILED" in format_policy(failed)
    assert "statusReasons=" in format_policy(failed)
    assert policy_statement_text({"definition": {"policy": {"statement": statement}}}) == statement


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


def test_tool_routes_match_the_operation_names():
    overrides = {item["name"]: item for item in tool_configuration()["toolOverrides"]}
    assert (overrides["read_artifacts"]["method"], overrides["read_artifacts"]["path"]) == ("GET", "/jobs/{jobId}")
    assert (overrides["read_test_results"]["method"], overrides["read_test_results"]["path"]) == ("GET", "/jobs/{jobId}/report")
    assert (overrides["request_spec_check"]["method"], overrides["request_spec_check"]["path"]) == ("POST", "/jobs")
    assert (overrides["approve"]["method"], overrides["approve"]["path"]) == ("POST", "/reviews")
    current = {"targetConfiguration": target_configuration("abc123")}
    assert target_config_matches(current, target_configuration("abc123")) is True
    shifted = target_configuration("abc123")
    shifted["mcp"]["apiGateway"]["apiGatewayToolConfiguration"]["toolOverrides"][1]["path"] = "/jobs"
    assert target_config_matches(current, shifted) is False


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
