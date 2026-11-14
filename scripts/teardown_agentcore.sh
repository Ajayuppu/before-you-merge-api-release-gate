#!/usr/bin/env bash
# Deletes only the AgentCore resources and roles created for this repo.
set -euo pipefail
if [[ "${CONFIRM_CLEANUP:-}" != "yes" && "${CONFIRM_AGENTCORE:-}" != "yes" ]]; then
  echo "Refusing to delete AgentCore resources."
  echo "After you mean to remove them: CONFIRM_CLEANUP=yes REGION=us-east-1 bash scripts/teardown_agentcore.sh"
  exit 2
fi
: "${REGION:?Set REGION}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 "$ROOT/scripts/agentcore_setup.py" teardown
