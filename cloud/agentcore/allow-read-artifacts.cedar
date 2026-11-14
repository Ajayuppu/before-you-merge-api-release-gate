permit (
  principal is AgentCore::IamEntity,
  action == AgentCore::Action::"ReviewTools___read_artifacts",
  resource == AgentCore::Gateway::"__GATEWAY_ARN__"
)
when {
  principal.id like "__PRINCIPAL_ID__*"
};
