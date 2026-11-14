#!/usr/bin/env bash
# Exercises the gateway allow, the policy deny, and one short code-interpreter run.
set -euo pipefail
if [[ "${CONFIRM_VALIDATE:-}" != "yes" ]]; then
  echo "Refusing to call AgentCore."
  echo "After authorization: CONFIRM_VALIDATE=yes REGION=us-east-1 bash scripts/validate_agentcore.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 "$ROOT/scripts/agentcore_setup.py" validate
