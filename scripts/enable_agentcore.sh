#!/usr/bin/env bash
# Optional AgentCore resources. The core stack does not create these.
# Order: code interpreter, policy engine, gateway, then Cedar permits that name that gateway.
set -euo pipefail
if [[ "${CONFIRM_AGENTCORE:-}" != "yes" ]]; then
  echo "Refusing to create AgentCore resources."
  echo "The core stack keeps SPEC_CHECK_MODE=in-process and BEDROCK_ADVISORY=disabled."
  echo "After authorization: CONFIRM_AGENTCORE=yes REGION=... EXECUTION_ROLE_ARN=... GATEWAY_ROLE_ARN=... bash scripts/enable_agentcore.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
: "${EXECUTION_ROLE_ARN:?Set EXECUTION_ROLE_ARN for the SANDBOX code interpreter}"
: "${GATEWAY_ROLE_ARN:?Set GATEWAY_ROLE_ARN for the MCP gateway}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
INTERPRETER_JSON="$(mktemp)"
python3 - "$EXECUTION_ROLE_ARN" "$INTERPRETER_JSON" <<'PY'
import json, pathlib, sys
role, destination = sys.argv[1:]
document = json.loads(pathlib.Path("cloud/agentcore/create_code_interpreter.json").read_text())
document["executionRoleArn"] = role
pathlib.Path(destination).write_text(json.dumps(document))
PY
aws bedrock-agentcore-control create-code-interpreter --region "$REGION" --cli-input-json "file://${INTERPRETER_JSON}"
ENGINE_JSON="$(aws bedrock-agentcore-control create-policy-engine --region "$REGION" --name ApiReviewPolicy --description "Read-only tools for the API review agent")"
ENGINE_ARN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("policyEngineArn") or json.loads(sys.argv[1]).get("arn"))' "$ENGINE_JSON")"
ENGINE_ID="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["policyEngineId"])' "$ENGINE_JSON")"
GATEWAY_JSON="$(mktemp)"
python3 - "$GATEWAY_ROLE_ARN" "$ENGINE_ARN" "$GATEWAY_JSON" <<'PY'
import json, pathlib, sys
role, engine, destination = sys.argv[1:]
document = json.loads(pathlib.Path("cloud/agentcore/create_gateway.json").read_text())
document["roleArn"] = role
document["policyEngineConfiguration"]["arn"] = engine
pathlib.Path(destination).write_text(json.dumps(document))
PY
GATEWAY_RESPONSE="$(aws bedrock-agentcore-control create-gateway --region "$REGION" --cli-input-json "file://${GATEWAY_JSON}")"
GATEWAY_ARN="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["gatewayArn"])' "$GATEWAY_RESPONSE")"
export GATEWAY_ARN
python3 - "$REGION" "$ENGINE_ID" "$GATEWAY_ARN" <<'PY'
import json, pathlib, subprocess, sys
region, engine, gateway = sys.argv[1:]
files = [
    ("AllowReadArtifacts", "cloud/agentcore/allow-read-artifacts.cedar"),
    ("AllowRequestSpecCheck", "cloud/agentcore/allow-request-spec-check.cedar"),
    ("AllowReadTestResults", "cloud/agentcore/allow-read-test-results.cedar"),
]
for name, path in files:
    statement = pathlib.Path(path).read_text().replace("__GATEWAY_ARN__", gateway)
    subprocess.run(
        [
            "aws", "bedrock-agentcore-control", "create-policy",
            "--region", region,
            "--policy-engine-id", engine,
            "--name", name,
            "--validation-mode", "FAIL_ON_ANY_FINDINGS",
            "--enforcement-mode", "ACTIVE",
            "--definition", json.dumps({"cedar": {"statement": statement}}),
        ],
        check=True,
    )
PY
echo "AgentCore gateway: ${GATEWAY_ARN}"
echo "Set the runtime to call only read_artifacts, request_spec_check, and read_test_results."
echo "Do not add an approve, merge, or deploy tool."
