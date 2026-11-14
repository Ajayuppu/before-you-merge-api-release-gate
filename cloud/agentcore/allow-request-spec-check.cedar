permit (
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"ReviewTools___request_spec_check",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.id like "__PRINCIPAL_ID__*"
};
