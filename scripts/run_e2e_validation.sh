#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${XYN_BASE_URL:-http://seed.localhost}"
UI_URL="${XYN_UI_URL:-http://localhost}"
WORKSPACE_SLUG="${XYN_WORKSPACE_SLUG:-development}"
TIMEOUT_SECONDS="${XYN_E2E_TIMEOUT_SECONDS:-240}"

PASS_COUNT=0
FAIL_COUNT=0
REPORT_LINES=()

pass() {
  local step="$1"
  REPORT_LINES+=("PASS | ${step}")
  PASS_COUNT=$((PASS_COUNT + 1))
}

fail() {
  local step="$1"
  local detail="${2:-}"
  REPORT_LINES+=("FAIL | ${step}${detail:+ | ${detail}}")
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1"
    exit 1
  }
}

wait_for_job_chain() {
  local workspace_id="$1"
  local deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    local jobs_json
    jobs_json="$(curl -fsS "${BASE_URL}/api/v1/jobs?workspace_id=${workspace_id}" -H "X-Workspace-Slug: ${WORKSPACE_SLUG}")" || true
    local gen dep prov smoke
    gen="$(echo "$jobs_json" | jq -r '[.[] | select(.type=="generate_app_spec")][0].status // ""')"
    dep="$(echo "$jobs_json" | jq -r '[.[] | select(.type=="deploy_app_local")][0].status // ""')"
    prov="$(echo "$jobs_json" | jq -r '[.[] | select(.type=="provision_sibling_xyn")][0].status // ""')"
    smoke="$(echo "$jobs_json" | jq -r '[.[] | select(.type=="smoke_test")][0].status // ""')"
    if [ "$gen" = "succeeded" ] && [ "$dep" = "succeeded" ] && [ "$prov" = "succeeded" ] && [ "$smoke" = "succeeded" ]; then
      echo "$jobs_json" >/tmp/xyn_jobs_chain.json
      return 0
    fi
    sleep 2
  done
  return 1
}

submit_draft_for_workspace() {
  local workspace_id="$1"
  curl -fsS -X POST "${BASE_URL}/api/v1/drafts?workspace_id=${workspace_id}" \
    -H "Content-Type: application/json" \
    -H "X-Workspace-Slug: ${WORKSPACE_SLUG}" \
    -d '{"type":"app_intent","title":"E2E Network Inventory","content_json":{"raw_prompt":"Build a network inventory app. It stores devices per workspace with locations. Provide the palette command show devices."}}' \
    >/tmp/e2e_draft.json
  local draft_id
  draft_id="$(jq -r '.id' /tmp/e2e_draft.json)"
  curl -fsS -X POST "${BASE_URL}/api/v1/drafts/${draft_id}/submit?workspace_id=${workspace_id}" \
    -H "X-Workspace-Slug: ${WORKSPACE_SLUG}" >/tmp/e2e_submit.json
}

workspace_id_by_slug() {
  local slug="$1"
  curl -fsS "${BASE_URL}/api/v1/workspaces" | jq -r --arg slug "$slug" '.[] | select(.slug==$slug) | .id' | head -n1
}

ensure_workspace() {
  local slug="$1"
  local title="$2"
  local existing
  existing="$(workspace_id_by_slug "$slug")"
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    echo "$existing"
    return 0
  fi
  curl -fsS -X POST "${BASE_URL}/api/v1/workspaces" \
    -H "Content-Type: application/json" \
    -d "{\"slug\":\"${slug}\",\"title\":\"${title}\"}" >/tmp/ws_create_"${slug}".json
  jq -r '.id' /tmp/ws_create_"${slug}".json
}

require_cmd curl
require_cmd jq
require_cmd python3
require_cmd docker

echo "Running Xyn E2E validation against ${BASE_URL}"

wait_http_ok() {
  local url="$1"
  local timeout="${2:-90}"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_device_visible() {
  local app_url="$1"
  local workspace_id="$2"
  local device_name="$3"
  local timeout="${4:-120}"
  local deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if curl -fsS "${app_url}/devices?workspace_id=${workspace_id}" 2>/dev/null | jq -e --arg name "${device_name}" '.items[]? | select(.name==$name)' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

resolve_app_url_from_container() {
  local container_name="$1"
  local host_port
  host_port="$(docker port "${container_name}" 8080/tcp | head -n1 | sed -E 's#.*:([0-9]+)$#\1#')"
  if [ -z "${host_port}" ]; then
    return 1
  fi
  echo "http://localhost:${host_port}"
}

if curl -fsS "${BASE_URL}/health" >/tmp/xyn_health.json; then
  pass "core health reachable"
else
  fail "core health reachable" "GET ${BASE_URL}/health failed"
fi

if curl -fsS -I "${UI_URL}" >/tmp/xyn_ui_head.txt; then
  pass "root UI reachable"
else
  fail "root UI reachable" "GET ${UI_URL} failed"
fi

WORKSPACE_ID="$(workspace_id_by_slug "${WORKSPACE_SLUG}")"
if [ -z "${WORKSPACE_ID}" ] || [ "${WORKSPACE_ID}" = "null" ]; then
  WORKSPACE_ID="$(ensure_workspace "${WORKSPACE_SLUG}" "Default Workspace")"
fi
echo "Workspace: ${WORKSPACE_SLUG} (${WORKSPACE_ID})"

# Ensure at least one successful deployment exists; create draft+submit if not.
if ! curl -fsS "${BASE_URL}/api/v1/jobs?workspace_id=${WORKSPACE_ID}" -H "X-Workspace-Slug: ${WORKSPACE_SLUG}" \
  | jq -e '.[] | select(.type=="deploy_app_local" and .status=="succeeded")' >/dev/null; then
  submit_draft_for_workspace "${WORKSPACE_ID}"
fi

if wait_for_job_chain "${WORKSPACE_ID}"; then
  pass "draft submit job chain"
else
  fail "draft submit job chain" "jobs did not complete successfully"
fi

APP_URL="$(jq -r '[.[] | select(.type=="smoke_test" and .status=="succeeded")][0].output_json.palette.meta.app_url // [.[] | select(.type=="deploy_app_local" and .status=="succeeded")][0].output_json.app_url // ""' /tmp/xyn_jobs_chain.json)"
SIBLING_UI_URL="$(jq -r '[.[] | select(.type=="smoke_test" and .status=="succeeded")][0].output_json.sibling_xyn.ui_url // ""' /tmp/xyn_jobs_chain.json)"
SIBLING_API_URL="$(jq -r '[.[] | select(.type=="smoke_test" and .status=="succeeded")][0].output_json.sibling_xyn.api_url // ""' /tmp/xyn_jobs_chain.json)"
APP_PORT="$(echo "${APP_URL}" | sed -E 's#^http://[^:]+:([0-9]+)$#\1#')"
APP_CONTAINER="$(docker ps --format '{{.Names}} {{.Ports}}' | awk -v port="${APP_PORT}" '$0 ~ (":" port "->8080/tcp") {print $1; exit}')"
DB_CONTAINER="${APP_CONTAINER%-api}-db"

if [ -z "${APP_URL}" ] || ! curl -fsS "${APP_URL}/health" >/tmp/net_inventory_health_bootstrap.json 2>/dev/null; then
  submit_draft_for_workspace "${WORKSPACE_ID}"
  if wait_for_job_chain "${WORKSPACE_ID}"; then
    APP_URL="$(jq -r '[.[] | select(.type=="smoke_test")][0].output_json.palette.meta.app_url // ""' /tmp/xyn_jobs_chain.json)"
  fi
fi

if [ -z "${APP_URL}" ] || ! curl -fsS "${APP_URL}/health" >/tmp/net_inventory_health_bootstrap.json 2>/dev/null; then
  fail "resolve net-inventory deployment URL" "missing or unreachable app URL from job output"
  echo "Aborting E2E run: net-inventory app URL unavailable."
  exit 1
fi

echo "App URL: ${APP_URL}"
echo "Sibling UI URL: ${SIBLING_UI_URL}"
echo "Sibling API URL: ${SIBLING_API_URL}"

if python3 scripts/validate_contracts.py \
  --contract contracts/core-api.json \
  --base-url "${BASE_URL}" \
  --workspace-id "${WORKSPACE_ID}" \
  --workspace-slug "${WORKSPACE_SLUG}" \
  >/tmp/contracts_core.txt 2>&1; then
  pass "core contract validation"
else
  fail "core contract validation" "$(tail -n 3 /tmp/contracts_core.txt | tr '\n' ' ')"
fi

if python3 scripts/validate_contracts.py \
  --contract contracts/net-inventory-api.json \
  --base-url "${APP_URL}" \
  --workspace-id "${WORKSPACE_ID}" \
  --workspace-slug "${WORKSPACE_SLUG}" \
  >/tmp/contracts_net_inventory.txt 2>&1; then
  pass "net-inventory-api contract validation"
else
  fail "net-inventory-api contract validation" "$(tail -n 3 /tmp/contracts_net_inventory.txt | tr '\n' ' ')"
fi

# Workspace isolation
WSA_ID="$(ensure_workspace "iso-a" "Isolation A")"
WSB_ID="$(ensure_workspace "iso-b" "Isolation B")"
curl -fsS -X POST "${APP_URL}/devices" -H "Content-Type: application/json" \
  -d "{\"workspace_id\":\"${WSA_ID}\",\"name\":\"iso-device-a1\",\"kind\":\"router\",\"status\":\"online\"}" >/tmp/iso_a_create.json
curl -fsS -X POST "${APP_URL}/devices" -H "Content-Type: application/json" \
  -d "{\"workspace_id\":\"${WSB_ID}\",\"name\":\"iso-device-b1\",\"kind\":\"switch\",\"status\":\"online\"}" >/tmp/iso_b_create.json

PA_A="$(curl -fsS -X POST "${BASE_URL}/api/v1/palette/execute" -H "Content-Type: application/json" -H "X-Workspace-Id: ${WSA_ID}" -d "{\"workspace_id\":\"${WSA_ID}\",\"prompt\":\"show devices\"}")"
PA_B="$(curl -fsS -X POST "${BASE_URL}/api/v1/palette/execute" -H "Content-Type: application/json" -H "X-Workspace-Id: ${WSB_ID}" -d "{\"workspace_id\":\"${WSB_ID}\",\"prompt\":\"show devices\"}")"
if echo "${PA_A}" | jq -e '.rows[]? | select(.name=="iso-device-a1")' >/dev/null && \
   ! echo "${PA_A}" | jq -e '.rows[]? | select(.name=="iso-device-b1")' >/dev/null && \
   echo "${PA_B}" | jq -e '.rows[]? | select(.name=="iso-device-b1")' >/dev/null && \
   ! echo "${PA_B}" | jq -e '.rows[]? | select(.name=="iso-device-a1")' >/dev/null; then
  pass "workspace isolation"
else
  fail "workspace isolation" "cross-workspace device leakage detected"
fi

# Persistence / restart
curl -fsS -X POST "${APP_URL}/devices" -H "Content-Type: application/json" \
  -d "{\"workspace_id\":\"${WORKSPACE_ID}\",\"name\":\"persist-device-1\",\"kind\":\"host\",\"status\":\"online\"}" >/tmp/persist_create.json

if wait_device_visible "${APP_URL}" "${WORKSPACE_ID}" "persist-device-1" 30; then
  if [ -z "${APP_CONTAINER}" ] || [ -z "${DB_CONTAINER}" ]; then
    fail "persistence after service/database restart" "unable to resolve app/db containers from app URL"
  elif docker restart "${APP_CONTAINER}" >/tmp/restart_app.txt && \
       APP_URL="$(resolve_app_url_from_container "${APP_CONTAINER}")" && \
       wait_http_ok "${APP_URL}/health" 90 && \
       wait_device_visible "${APP_URL}" "${WORKSPACE_ID}" "persist-device-1" 120; then
    if docker restart "${DB_CONTAINER}" >/tmp/restart_db.txt && \
       APP_URL="$(resolve_app_url_from_container "${APP_CONTAINER}")" && \
       wait_http_ok "${APP_URL}/health" 120 && \
       wait_device_visible "${APP_URL}" "${WORKSPACE_ID}" "persist-device-1" 180; then
      pass "persistence after service/database restart"
    else
      fail "persistence after service/database restart" "device missing after db restart"
    fi
  else
    fail "persistence after service/database restart" "device missing after api restart"
  fi
else
  fail "persistence after service/database restart" "failed to create/verify seed device"
fi

# Palette command registry check
CMDS_JSON="$(curl -fsS "${BASE_URL}/api/v1/palette/commands?workspace_id=${WORKSPACE_ID}" -H "X-Workspace-Slug: ${WORKSPACE_SLUG}")"
if echo "${CMDS_JSON}" | jq -e '.[] | select(.command_key=="show devices")' >/dev/null && \
   curl -fsS -X POST "${BASE_URL}/api/v1/palette/execute" -H "Content-Type: application/json" -H "X-Workspace-Slug: ${WORKSPACE_SLUG}" \
   -d "{\"workspace_id\":\"${WORKSPACE_ID}\",\"prompt\":\"show devices\"}" | jq -e '.kind=="table"' >/dev/null; then
  pass "palette command registry execution"
else
  fail "palette command registry execution" "show devices command missing or execution failed"
fi

# Artifact refresh/self-update smoke
ARTIFACT_CHANNEL="${XYN_ARTIFACT_CHANNEL:-dev}"
if curl -fsS -X POST "${BASE_URL}/api/v1/artifacts/refresh" -H "Content-Type: application/json" \
  -d "{\"artifacts\":[\"xyn-ui\",\"xyn-api\",\"net-inventory-api\"],\"channel\":\"${ARTIFACT_CHANNEL}\"}" >/tmp/artifact_refresh_e2e.json && \
  curl -fsS "${BASE_URL}/health" >/tmp/health_after_refresh.json && \
  curl -fsS -I "${UI_URL}" >/tmp/ui_after_refresh.txt; then
  pass "artifact refresh/self-update smoke"
else
  fail "artifact refresh/self-update smoke" "refresh or post-refresh health check failed"
fi

echo
echo "=== E2E REPORT ==="
echo "Root API: ${BASE_URL}"
echo "Root UI: ${UI_URL}"
echo "Sibling UI: ${SIBLING_UI_URL}"
echo "Sibling API: ${SIBLING_API_URL}"
echo "net-inventory API: ${APP_URL}"
echo "Workspace ID: ${WORKSPACE_ID}"
printf '%s\n' "${REPORT_LINES[@]}"
echo "TOTAL PASS=${PASS_COUNT} FAIL=${FAIL_COUNT}"

if [ "$FAIL_COUNT" -gt 0 ]; then
  exit 1
fi
