#!/usr/bin/env bash
# Optional AgentCore resources. The core stack does not create these.
set -euo pipefail
if [[ "${CONFIRM_AGENTCORE:-}" != "yes" ]]; then
  echo "Refusing to create AgentCore resources."
  echo "The core stack keeps SPEC_CHECK_MODE=in-process and BEDROCK_ADVISORY=disabled."
  echo "After authorization: CONFIRM_AGENTCORE=yes REGION=us-east-1 bash scripts/enable_agentcore.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 "$ROOT/scripts/agentcore_setup.py" enable
