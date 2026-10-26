#!/usr/bin/env bash
# End-to-end demo: real LLM discovery -> artifact -> replays (success, business outcome,
# recoverable, hard failure, human handoff). Writes everything under evidence/.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY (discovery is a real model run)}"
export CUA_SECRET_OPERATOR_ID=teller01 CUA_SECRET_OPERATOR_PASSWORD=demo-pass-123   # mock app's demo creds
RUNS=evidence/runs; mkdir -p "$RUNS"

uv run uvicorn mock_bank.app:app --port 8000 --log-level warning & SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT
sleep 2

echo "== 1. discovery (LLM)"
uv run cua --runs $RUNS discover --target http://127.0.0.1:8000/login \
  --goal "Look up member 12345 and read their current share savings balance" | tee evidence/discovery_result.json
ART=$(uv run python -c "import json;print(json.load(open('evidence/discovery_result.json'))['artifact'])")
ART_DIR=$(dirname "$ART")
cp "$ART" evidence/artifact.yaml
uv run cua show "$ART_DIR"
# the model names the input; read it from the artifact rather than assuming
P=$(uv run python -c "from cua.store import load_capability as l;print(l('$ART_DIR').inputs[0].name)")

replay() { local name=$1; shift; echo "== replay: $name"; uv run cua --runs $RUNS replay "$ART_DIR" "$@" | tee "evidence/replay_${name}.json" || true; }
replay success_other_member   --param $P=23456
replay not_found              --param $P=99999
replay access_denied          --param $P=34567
replay invalid_input          --param $P=12A45
replay recover_notice         --param $P=12345 --fault dialog
replay recover_slow           --param $P=12345 --fault slow
replay hard_failure           --param $P=12345 --fault app_error

echo "== replay: handoff (session expires mid-run; operator re-signs on in the live session)"
uv run python scripts/operator_relogin.py $RUNS & OP=$!
replay handoff --param $P=12345 --fault session_expired --handoff
wait $OP
echo "done -> evidence/"
