#!/usr/bin/env bash
set -euo pipefail

# deep_hierarchy_smoke.sh
# Purpose: Create a deep agent hierarchy, seed conflicts and exercise core API flows
# Usage: ./examples/deep_hierarchy_smoke.sh
# Notes:
# - Run inside a virtualenv; install deps with `pip install -r requirements.txt` first.
# - The script starts the FastAPI server in background (uvicorn) and uses curl to exercise endpoints.
# - Adjust LANGBRAIN_HOST/LANGBRAIN_PORT as needed.

HOST="127.0.0.1"
PORT=8000
BASE="http://${HOST}:${PORT}"
UVICORN_LOG=dev_uvicorn.log
SERVER_PID_FILE=.langbrain_server.pid

echo "[+] Ensure requirements are installed (use venv)."
echo "    pip install -r requirements.txt"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl required. Install and re-run."
  exit 1
fi

# Start server
if [ -f "$SERVER_PID_FILE" ]; then
  OLD_PID=$(cat "$SERVER_PID_FILE")
  if kill -0 "$OLD_PID" >/dev/null 2>&1; then
    echo "Server appears already running (pid=$OLD_PID). Skipping start."
  else
    rm -f "$SERVER_PID_FILE"
  fi
fi

if [ ! -f "$SERVER_PID_FILE" ]; then
  echo "[+] Starting uvicorn server in background..."
  python3 -m uvicorn app.api.main:app --host ${HOST} --port ${PORT} --reload &> "$UVICORN_LOG" &
  PID=$!
  echo $PID > "$SERVER_PID_FILE"
  echo "  -> pid=$PID"
fi

# wait for server
echo "[+] Waiting for server to be ready on ${BASE}/"
for i in {1..30}; do
  if curl -sSf "${BASE}/" >/dev/null 2>&1; then
    echo "  server is up"
    break
  fi
  sleep 1
done

# Reset system DB/state via API if available
echo "[+] Attempting system reset via ${BASE}/system/reset"
if curl -sS -X DELETE "${BASE}/system/reset" -H 'Content-Type: application/json' -o /dev/null; then
  echo "  system reset requested (OK)"
else
  echo "  system reset endpoint not available or failed; continuing anyway"
fi

# Helper to post JSON
post() {
  local url="$1"; shift
  curl -sS -X POST "$url" -H 'Content-Type: application/json' -d "$@"
}

# 1) Create a deep hierarchy: Brain -> organ_1 -> comp_1 -> subcomp_1 -> subcomp_2 ...
# We'll create multiple nested agents to exercise recursion and registry.

echo "[+] Creating deep hierarchy (6 levels)"

create_agent() {
  local name="$1"; local level="$2"; local parent="$3"; local targets_json="$4"; local weight="$5"
  PAYLOAD=$(jq -n --argjson mt "$targets_json" --arg name "$name" --arg parent "$parent" --argjson level "$level" --arg weight "$weight" '{"name": $name, "level": $level, "parent_agent_name": $parent, "managed_targets": $mt, "sub_agent_names": [], "priority_weight": ($weight|tonumber) }')
  post "${BASE}/agents/create" "$(jq -c -M -n --arg s "$PAYLOAD" '{agent_definition: $s}')"
}

# requires jq
if ! command -v jq >/dev/null 2>&1; then
  echo "Please install 'jq' to run this script (sudo apt install jq)"
  exit 1
fi

# Chain creation
create_agent "organ_level1" 1 "Brain" '["device_l1"]' 10.0
create_agent "component_level2" 2 "organ_level1" '["device_l2"]' 20.0
create_agent "subcomponent_level3" 3 "component_level2" '["device_l3"]' 30.0
create_agent "leaf_level4" 4 "subcomponent_level3" '["device_l4"]' 40.0
create_agent "leaf_level5" 5 "leaf_level4" '["device_l5"]' 50.0
create_agent "leaf_level6" 6 "leaf_level5" '["device_l6"]' 60.0

echo "[+] Agents created. Listing agents to verify"
curl -sS "${BASE}/agents" | jq '.'

# 2) On-demand tool creation and seed conflict
echo "[+] Seeding conflicts and creating tools on-demand"
curl -sS -X POST "${BASE}/events/seed-conflict" -H 'Content-Type: application/json' -d '{"actor": "organ_security", "action": "FORCE_SHUTDOWN", "target": "device_l3", "old_value": "ON", "new_value": "OFF", "reasoning": "test conflict"}' | jq '.'

# set an explicit tool value via API
curl -sS -X POST "${BASE}/tools" -H 'Content-Type: application/json' -d '{"target": "device_l3", "value": "OFF"}' | jq '.'

# 3) Run a graph cycle forcing child agent to run and create escalation
echo "[+] Running graph cycle with sensor reading to start at deepest branch"
READINGS='[{"sensor_id":"device_l3","agent_owner":"api","value":"75","unit":""}]'
JSON_PAYLOAD=$(cat <<JSON
{"sensor_readings": $READINGS, "force_next_agent": "brain"}
JSON
)
curl -sS -X POST "${BASE}/graph/run" -H 'Content-Type: application/json' -d "$JSON_PAYLOAD" | jq '.'

echo "[+] Enabling HITL (interrupt before nodes) to exercise resume/override"
curl -sS -X POST "${BASE}/hitl/config" -H 'Content-Type: application/json' -d '{"hitl_all": true, "hitl_nodes": ["brain"], "hitl_targets": [], "hitl_actions": [], "max_wait_seconds": 10, "allow_override": true}' | jq '.'

echo "[+] Running graph cycle again (should trigger HITL interrupt on brain)"
JSON_PAYLOAD=$(cat <<JSON
{"sensor_readings": $READINGS, "force_next_agent": "brain"}
JSON
)
curl -sS -X POST "${BASE}/graph/run" -H 'Content-Type: application/json' -d "$JSON_PAYLOAD" | jq '.' || true

echo "[+] Inspect graph state (should show interrupted tasks)"
curl -sS "${BASE}/graph/state" | jq '.'

echo "[+] Performing OVERRIDE via /graph/resume (semantic override -> fallback UNBLOCK_AND_SET)"
curl -sS -X POST "${BASE}/graph/resume" -H 'Content-Type: application/json' -d '{"decision": "OVERRIDE", "reasoning": "Force set device_l3 ON and device_l4 OFF for test", "thread_id": "api_session"}' | jq '.'

echo "[+] Check tools after override"
curl -sS "${BASE}/tools/device_l3" | jq '.'
curl -sS "${BASE}/tools/device_l4" | jq '.'

echo "[+] Health check"
curl -sS -X POST "${BASE}/graph/health-check" | jq '.'

echo "[+] Recent events"
curl -sS "${BASE}/events" | jq '.'

echo "[+] Cleanup: disable HITL and stop server"
curl -sS -X POST "${BASE}/hitl/config" -H 'Content-Type: application/json' -d '{"hitl_all": false, "hitl_nodes": [], "hitl_targets": [], "hitl_actions": [], "max_wait_seconds": null, "allow_override": true}' | jq '.'

if [ -f "$SERVER_PID_FILE" ]; then
  PID=$(cat "$SERVER_PID_FILE")
  echo "Stopping server pid=$PID"
  kill "$PID" || true
  rm -f "$SERVER_PID_FILE"
fi

echo "[+] Deep hierarchy smoke finished. Logs: $UVICORN_LOG"