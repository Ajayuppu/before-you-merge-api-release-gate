#!/usr/bin/env bash
# Read-only tracing check. It does not submit a job or change any resource.
set -euo pipefail
: "${REGION:?Set REGION}"
STACK="${STACK_NAME:-api-review}"
PREFIX="${PROJECT_NAME:-api-review}"

echo "Lambda functions"
mapfile -t FUNCTIONS < <(aws lambda list-functions --region "$REGION" --query "Functions[?starts_with(FunctionName, '${PREFIX}-')].FunctionName" --output text | tr '\t' '\n' | sed '/^$/d')
if [[ ${#FUNCTIONS[@]} -eq 0 ]]; then
  echo "No functions named ${PREFIX}-*."
else
for name in "${FUNCTIONS[@]}"; do
  echo "function ${name}"
  aws lambda get-function-configuration --region "$REGION" --function-name "$name" --query "{TracingConfig:TracingConfig,Environment:{AWS_LAMBDA_EXEC_WRAPPER:Environment.Variables.AWS_LAMBDA_EXEC_WRAPPER,OTEL_AWS_APPLICATION_SIGNALS_ENABLED:Environment.Variables.OTEL_AWS_APPLICATION_SIGNALS_ENABLED,OTEL_EXPORTER_OTLP_PROTOCOL:Environment.Variables.OTEL_EXPORTER_OTLP_PROTOCOL,OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:Environment.Variables.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT,OTEL_LOGS_EXPORTER:Environment.Variables.OTEL_LOGS_EXPORTER,OTEL_METRICS_EXPORTER:Environment.Variables.OTEL_METRICS_EXPORTER,OTEL_PROPAGATORS:Environment.Variables.OTEL_PROPAGATORS,OTEL_PYTHON_CONFIGURATOR:Environment.Variables.OTEL_PYTHON_CONFIGURATOR,OTEL_PYTHON_DISTRO:Environment.Variables.OTEL_PYTHON_DISTRO,OTEL_SERVICE_NAME:Environment.Variables.OTEL_SERVICE_NAME,OTEL_TRACES_EXPORTER:Environment.Variables.OTEL_TRACES_EXPORTER,OTEL_TRACES_SAMPLER:Environment.Variables.OTEL_TRACES_SAMPLER}}" --output json
done
fi

echo "API Gateway stages"
if outputs="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" --query "Stacks[0].Outputs" --output json 2>/dev/null)"; then
  python3 - "$REGION" "$outputs" <<'PY'
import json, subprocess, sys
from urllib.parse import urlparse
region, raw = sys.argv[1], sys.argv[2]
rows = json.loads(raw) or []
for key in ("ReviewApiEndpoint", "MockApiEndpoint"):
    endpoint = next((row["OutputValue"] for row in rows if row["OutputKey"] == key), "")
    host = urlparse(endpoint).hostname or ""
    api_id = host.split(".")[0] if host else ""
    print(f"{key} {api_id or 'missing'}")
    if not api_id:
        continue
    subprocess.run(["aws", "apigateway", "get-stages", "--region", region, "--rest-api-id", api_id, "--query", "item[].{stage:stageName,tracing:tracingEnabled}", "--output", "json"], check=False)
PY
else
  echo "Stack ${STACK} was not found, so API stage tracing was not read."
fi

echo "Step Functions"
if arn="$(aws stepfunctions list-state-machines --region "$REGION" --query "stateMachines[?name=='${PREFIX}'].stateMachineArn | [0]" --output text 2>/dev/null)" && [[ -n "$arn" && "$arn" != "None" ]]; then
  aws stepfunctions describe-state-machine --region "$REGION" --state-machine-arn "$arn" --query "{name:name,tracing:tracingConfiguration}" --output json
else
  echo "State machine ${PREFIX} was not found."
fi

END="$(date +%s)"
START="$((END - 1800))"
echo "X-Ray trace summaries from ${START} to ${END}"
SUMMARIES="$(aws xray get-trace-summaries --region "$REGION" --start-time "$START" --end-time "$END" --output json)"
python3 -c 'import json,sys; payload=json.loads(sys.argv[1]); rows=payload.get("TraceSummaries") or []; print(f"trace summaries {len(rows)}")' "$SUMMARIES"

echo "X-Ray indexing rules"
if ! aws xray get-indexing-rules --region "$REGION" --output json; then
  echo "aws xray get-indexing-rules is not available."
fi
echo "X-Ray trace segment destination"
if ! aws xray get-trace-segment-destination --region "$REGION" --output json; then
  echo "aws xray get-trace-segment-destination is not available."
fi
echo "This check did not submit a job."
