#!/usr/bin/env bash
# Entrypoint contract for the agent image — see docs/AGENT_CONTRACT.md.
#
# Everything this script needs comes from the environment, which the dispatcher
# fills in. Nothing here reads the job payload directly, so a job can never
# influence how the container is built or what it is allowed to reach.
set -euo pipefail

: "${JOB_ID:?JOB_ID is required}"
: "${TASK:?TASK is required}"

WORKSPACE="${WORKSPACE:-/workspace}"
REPO_DIR_NAME="${REPO_DIR_NAME:-repo}"
WORKSPACE_FOLDER="${WORKSPACE}/${REPO_DIR_NAME}"
RESULT_MARKER="::agent-result::"
JOB_LABEL="agent-sandbox.job=${JOB_ID}"

status="failed"
exit_code=1
summary="the agent exited before reporting a result"

emit_result() {
  # Runs on every exit path, so the dispatcher always has exactly one result
  # line to extract, even when the job dies halfway through.
  local payload
  payload="$(jq -cn \
    --arg jobId "${JOB_ID}" \
    --arg status "${status}" \
    --argjson exitCode "${exit_code}" \
    --arg summary "${summary}" \
    '{jobId: $jobId, status: $status, exitCode: $exitCode, summary: $summary}')"
  printf '%s%s\n' "${RESULT_MARKER}" "${payload}"
}

cleanup() {
  # The devcontainer runs on the remote daemon and outlives this container
  # unless it is removed explicitly. Failures here must not mask the job result.
  #
  # A job that runs its command here started no devcontainer, and the dispatcher
  # gives it no Docker access at all, so there is nothing to sweep and nothing
  # to sweep it with.
  [ "${AGENT_CMD_LOCATION:-devcontainer}" = "devcontainer" ] || return 0
  local ids
  ids="$(docker ps -aq --filter "label=${JOB_LABEL}" 2>/dev/null || true)"
  if [ -n "${ids}" ]; then
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null 2>&1 || true
  fi
}

on_exit() {
  cleanup
  emit_result
}
trap on_exit EXIT

log() { printf 'agent: %s\n' "$*"; }

log "job ${JOB_ID} starting"
log "docker host: ${DOCKER_HOST:-<default socket>}"

# --- workspace ---------------------------------------------------------------

if [ -n "${REPO_URL:-}" ]; then
  log "cloning ${REPO_URL} into ${WORKSPACE_FOLDER}"
  rm -rf "${WORKSPACE_FOLDER}"
  if [ -n "${REPO_REF:-}" ]; then
    git clone --depth 1 --branch "${REPO_REF}" "${REPO_URL}" "${WORKSPACE_FOLDER}"
  else
    git clone --depth 1 "${REPO_URL}" "${WORKSPACE_FOLDER}"
  fi
else
  log "no REPO_URL given, using an empty workspace"
  mkdir -p "${WORKSPACE_FOLDER}"
fi

# The task is handed over as a file rather than only as an environment variable,
# so an agent CLI can read it without the dispatcher having to quote it into a
# command line.
printf '%s\n' "${TASK}" > "${WORKSPACE_FOLDER}/AGENT_TASK.md"

# --- agent ------------------------------------------------------------------
#
# AGENT_CMD_LOCATION decides where the command runs, and that decides whether a
# devcontainer is needed at all:
#
#   devcontainer  (default) bring one up and run the command inside it, so the
#                 command sees the repository's own toolchain.
#   agent         run the command here, in this container. For an agent CLI that
#                 is baked into the image and brings its own tooling — Claude
#                 Code in the `claude` build target — a devcontainer would be a
#                 container started for nothing.

AGENT_CMD_LOCATION="${AGENT_CMD_LOCATION:-devcontainer}"
case "${AGENT_CMD_LOCATION}" in
  devcontainer|agent) ;;
  *)
    summary="AGENT_CMD_LOCATION must be 'devcontainer' or 'agent', got '${AGENT_CMD_LOCATION}'"
    log "${summary}"
    exit 2
    ;;
esac

if [ "${AGENT_CMD_LOCATION}" = "agent" ]; then
  if [ -z "${AGENT_CMD:-}" ]; then
    summary="AGENT_CMD_LOCATION=agent needs an AGENT_CMD; there is nothing else to run here"
    log "${summary}"
    exit 2
  fi
  log "running the agent command in the agent container"
  cd "${WORKSPACE_FOLDER}"
  set +e
  bash -lc "${AGENT_CMD}"
  exit_code=$?
  set -e
else
  override_config="${WORKSPACE_FOLDER}/.devcontainer/devcontainer.agent.json"
  node /usr/local/lib/agent/devcontainer-config.mjs \
    --workspace-folder "${WORKSPACE_FOLDER}" \
    --out "${override_config}" \
    ${WORKSPACE_VOLUME:+--volume "${WORKSPACE_VOLUME}"} \
    ${AGENT_DEVCONTAINER_IMAGE:+--default-image "${AGENT_DEVCONTAINER_IMAGE}"}

  log "bringing up the devcontainer"
  devcontainer up \
    --workspace-folder "${WORKSPACE_FOLDER}" \
    --override-config "${override_config}" \
    --id-label "${JOB_LABEL}" \
    --remove-existing-container

  if [ -z "${AGENT_CMD:-}" ]; then
    # No agent CLI is baked into the base image on purpose (docs/PLAN.md §6), so
    # a job without AGENT_CMD is a smoke test of the pipeline rather than an error.
    status="succeeded"
    exit_code=0
    summary="devcontainer is up; no AGENT_CMD was configured, so nothing was run inside it"
    log "${summary}"
    exit 0
  fi

  log "running the agent command inside the devcontainer"
  set +e
  devcontainer exec \
    --workspace-folder "${WORKSPACE_FOLDER}" \
    --override-config "${override_config}" \
    --id-label "${JOB_LABEL}" \
    -- bash -lc "${AGENT_CMD}"
  exit_code=$?
  set -e
fi

if [ "${exit_code}" -eq 0 ]; then
  status="succeeded"
  summary="the agent command finished successfully"
else
  status="failed"
  summary="the agent command exited with code ${exit_code}"
fi
log "${summary}"
exit "${exit_code}"
