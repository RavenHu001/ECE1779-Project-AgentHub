#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

mode=${1:---plan}
[[ $# -le 1 && ( $mode == --plan || $mode == --apply ) ]] || fail 'Usage: bash deploy/scripts/deploy.sh [--plan|--apply]'
configure
if [[ $mode == --plan ]]; then
  note "PLAN for $STACK (no daemon, registry or SSH calls; no changes):"
  note '1. Acquire the shared manager lock and run online preflight.'
  note '2. Deploy the existing stack with API/worker/setup at zero; wait for old application tasks to stop.'
  note '3. Wait for PostgreSQL health, then run exactly one setup task and require exit code zero.'
  note "4. Return setup to zero; start API=$API_REPLICAS and worker=$WORKER_REPLICAS and verify health on every node."
  note 'Existing deployments incur planned downtime. No data deletion, automatic migration retry, or schema rollback.'
  exit 0
fi

need flock
# Every operator on this manager must use the same lock file, including other checkouts.
exec 9>"$DEPLOY_LOCK_PATH" || fail 'Cannot open deployment lock; arrange manager permissions first'
flock -n 9 || fail 'Another deployment holds the manager lock'
stage=preflight
trap 'printf "Deployment stopped during %s. Inspect service/task state before retrying; no automatic recovery performed.\n" "$stage" >&2' ERR
online_preflight

target_api=$API_REPLICAS
target_worker=$WORKER_REPLICAS
stage=database-startup
API_REPLICAS=0 WORKER_REPLICAS=0 docker stack deploy --detach=true --resolve-image never --with-registry-auth -c "$STACK_FILE" "$STACK"
wait_stopped "${STACK}_api"
wait_stopped "${STACK}_worker"
wait_stopped "${STACK}_setup"
wait_healthy "${STACK}_db" 1

stage=migration-and-bootstrap
[[ $(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup") == 0 ]] || fail 'Setup must be at zero before launch'
[[ $(docker service inspect --format '{{.Spec.TaskTemplate.RestartPolicy.Condition}}' "${STACK}_setup") == none ]] || fail 'Setup must not restart automatically'
previous=$(docker service ps --no-trunc -q "${STACK}_setup")
declare -A seen=()
while IFS= read -r id; do [[ -z $id ]] || seen["$id"]=1; done <<< "$previous"
docker service update --detach=true --replicas 1 --force "${STACK}_setup"
deadline=$((SECONDS + DEPLOY_TIMEOUT))
selected=''; completed=false
while (( SECONDS < deadline )); do
  tasks=$(docker service ps --no-trunc -q "${STACK}_setup")
  while IFS= read -r id; do
    [[ -n $id && -z ${seen[$id]:-} ]] || continue
    [[ -z $selected || $selected == "$id" ]] || fail 'Multiple new setup tasks detected; stop for review'
    selected=$id
  done <<< "$tasks"
  if [[ -n $selected ]]; then
    state=$(docker inspect --type task --format '{{.Status.State}}' "$selected")
    case $state in
      complete)
        [[ $(docker inspect --type task --format '{{.Status.ContainerStatus.ExitCode}}' "$selected") == 0 ]] || fail 'Setup returned a nonzero exit code'
        completed=true; break ;;
      failed|rejected|orphaned|shutdown) fail "Setup ended unsuccessfully ($state); applications remain stopped" ;;
    esac
  fi
  sleep 2
done
[[ $completed == true ]] || fail 'Setup timed out; it may still be running. Do not restart it blindly.'
note 'Alembic migration and bootstrap completed with exit code 0.'
docker service scale --detach=true "${STACK}_setup=0"
wait_stopped "${STACK}_setup"

stage=application-startup
docker service scale --detach=true "${STACK}_api=$target_api" "${STACK}_worker=$target_worker"
wait_healthy "${STACK}_api" "$target_api"
wait_healthy "${STACK}_worker" "$target_worker"
note 'Deployment converged. Run the authenticated deployment smoke check separately.'
