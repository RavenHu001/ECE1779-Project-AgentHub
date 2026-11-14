#!/usr/bin/env bash
# Shared checks for the two-node deployment scripts. No actions run on source.

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
need() { command -v "$1" >/dev/null || fail "Required command missing: $1"; }

configure() {
  local name value
  for name in STACK AGENTHUB_IMAGE POSTGRES_IMAGE POSTGRES_DATA_PATH DB_PASSWORD_SECRET BOOTSTRAP_PASSWORD_SECRET POSTGRES_DB POSTGRES_USER BOOTSTRAP_USERNAME; do
    [[ -n ${!name:-} ]] || fail "Export $name; see deploy/swarm/config.example.env"
    export "$name"
  done
  export API_REPLICAS=${API_REPLICAS:-2} WORKER_REPLICAS=${WORKER_REPLICAS:-2}
  export API_PUBLISHED_PORT=${API_PUBLISHED_PORT:-18000}
  export IMAGE_MODE=${IMAGE_MODE:-registry} STORAGE_MODE=${STORAGE_MODE:-volume}
  export DEPLOY_TIMEOUT=${DEPLOY_TIMEOUT:-300}
  export DEPLOY_LOCK_PATH=${DEPLOY_LOCK_PATH:-/var/lock/agenthub-deploy.lock}
  for name in STACK DB_PASSWORD_SECRET BOOTSTRAP_PASSWORD_SECRET POSTGRES_DB POSTGRES_USER BOOTSTRAP_USERNAME; do
    value=${!name}
    [[ $value =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail "Invalid identifier in $name"
  done
  [[ $STACK =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail 'STACK must use lowercase letters, digits and hyphens'
  [[ $DB_PASSWORD_SECRET != "$BOOTSTRAP_PASSWORD_SECRET" ]] || fail 'Use two independent secrets'
  for name in API_REPLICAS WORKER_REPLICAS DEPLOY_TIMEOUT; do
    [[ ${!name} =~ ^[1-9][0-9]{0,3}$ ]] || fail "$name must be a positive integer (maximum 9999)"
  done
  [[ $API_PUBLISHED_PORT =~ ^[1-9][0-9]{0,4}$ ]] && (( API_PUBLISHED_PORT <= 65535 )) || fail 'Invalid API_PUBLISHED_PORT'
  for name in POSTGRES_DATA_PATH DEPLOY_LOCK_PATH; do
    [[ ${!name} =~ ^/[a-zA-Z0-9_./-]+$ && ${!name} != / && ${!name}/ != *'/../'* ]] || fail "$name must be an absolute path without spaces or parent traversal"
  done
  case $IMAGE_MODE in
    registry)
      [[ $AGENTHUB_IMAGE =~ ^[a-z0-9.-]+(:[0-9]+)?/.+@sha256:[a-f0-9]{64}$ ]] || fail 'Use a registry-qualified AGENTHUB_IMAGE digest'
      [[ $POSTGRES_IMAGE =~ ^[^[:space:]]+@sha256:[a-f0-9]{64}$ ]] || fail 'Pin POSTGRES_IMAGE by digest'
      ;;
    local)
      for value in "$AGENTHUB_IMAGE" "$POSTGRES_IMAGE"; do
        [[ $value =~ ^[a-zA-Z0-9][a-zA-Z0-9./:@_-]+$ ]] || fail 'Invalid local image reference'
        if [[ $value == *@* ]]; then
          [[ $value =~ @sha256:[a-f0-9]{64}$ ]] || fail 'Invalid local image digest'
        else
          [[ ${value##*/} == *:* && $value != *:latest && $value != *: ]] || fail 'Local images need explicit non-latest versions'
        fi
      done
      ;;
    *) fail 'IMAGE_MODE must be registry or local' ;;
  esac
  case $STORAGE_MODE in
    volume)
      [[ ${POSTGRES_VOLUME_MOUNT:-} =~ ^/[a-zA-Z0-9_./-]+$ && ${POSTGRES_VOLUME_MOUNT:-} != / ]] || fail 'Set POSTGRES_VOLUME_MOUNT to the Volume mount point'
      [[ ${POSTGRES_VOLUME_UUID:-} =~ ^[a-zA-Z0-9-]+$ ]] || fail 'Set POSTGRES_VOLUME_UUID to the filesystem UUID'
      [[ $POSTGRES_DATA_PATH == "$POSTGRES_VOLUME_MOUNT/"* ]] || fail 'Data directory must be below the Volume mount point'
      ;;
    directory) ;;
    *) fail 'STORAGE_MODE must be volume or directory' ;;
  esac
  need docker; need python3
  SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  STACK_FILE="$SCRIPT_DIR/../swarm/stack.yaml"
  # Never print rendered configuration or feed it through interpolation twice.
  docker stack config -c "$STACK_FILE" >/dev/null || fail 'Stack configuration is invalid'
}

on_node() {
  local node=$1 quoted
  shift
  if [[ $node == "$MANAGER_ID" ]]; then
    "$@"
  else
    [[ $node == "$WORKER_ID" && -n ${WORKER_SSH:-} ]] || fail 'Unknown remote node'
    printf -v quoted '%q ' "$@"
    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$WORKER_SSH" "$quoted"
  fi
}

task_states() {
  local ids
  ids=$(docker service ps --no-trunc -q "$1") || return 1
  [[ -n $ids ]] || return 0
  local -a tasks
  mapfile -t tasks <<< "$ids"
  docker inspect --type task --format '{{.Status.State}}' "${tasks[@]}"
}

stopped() {
  local states state
  states=$(task_states "$1") || return 1
  while IFS= read -r state; do
    case $state in ''|complete|shutdown|failed|rejected) ;; *) return 1 ;; esac
  done <<< "$states"
}

wait_stopped() {
  local service=$1 deadline=$((SECONDS + DEPLOY_TIMEOUT))
  until stopped "$service"; do
    (( SECONDS < deadline )) || fail "Tasks did not stop: $service. Resolve unfinished/orphaned tasks before retrying."
    sleep 2
  done
}

wait_healthy() {
  local service=$1 expected=$2 deadline=$((SECONDS + DEPLOY_TIMEOUT)) ids id record node container state count total health update
  while (( SECONDS < deadline )); do
    update=$(docker service inspect --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' "$service") || fail "Cannot inspect $service"
    case $update in paused|rollback*) fail "Update failed for $service; inspect task history privately" ;; esac
    ids=$(docker service ps --no-trunc -q --filter desired-state=running "$service") || fail "Cannot list $service tasks"
    count=0; total=0
    while IFS= read -r id; do
      [[ -n $id ]] || continue
      total=$((total + 1))
      record=$(docker inspect --type task --format '{{.NodeID}}|{{.Status.State}}|{{if .Status.ContainerStatus}}{{.Status.ContainerStatus.ContainerID}}{{end}}' "$id") || fail 'Cannot inspect task'
      IFS='|' read -r node state container <<< "$record"
      [[ $state == running && -n $container ]] || continue
      health=$(on_node "$node" docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container" 2>/dev/null) || health=unavailable
      [[ $health != healthy ]] || count=$((count + 1))
    done <<< "$ids"
    if (( count == expected && total == expected )) && [[ $update != updating ]]; then
      note "$service: $count healthy tasks"
      return
    fi
    sleep 2
  done
  fail "Health/convergence timeout for $service; no automatic retry or rollback performed"
}

inspect_cluster() {
  local endpoint node state label arch remote_id images image node_ids
  [[ $(uname -s) == Linux ]] || fail 'Run online checks on the native Linux manager, not Windows/Git Bash'
  [[ -z ${DOCKER_HOST:-} ]] || fail 'Unset DOCKER_HOST; run on the manager using its local Unix socket'
  endpoint=$(docker context inspect --format '{{.Endpoints.docker.Host}}') || fail 'Cannot inspect Docker context'
  [[ $endpoint == unix://* ]] || fail 'A local Unix-socket Docker context is required'
  [[ $(docker info --format '{{.OSType}} {{.Swarm.LocalNodeState}} {{.Swarm.ControlAvailable}}') == 'linux active true' ]] || fail 'An active Linux Swarm manager is required'
  [[ $(docker info --format '{{.OperatingSystem}}') != *'Docker Desktop'* ]] || fail 'Online automation requires a native Linux manager; use the manual Desktop rehearsal'
  MANAGER_ID=$(docker info --format '{{.Swarm.NodeID}}')
  node_ids=$(docker node ls -q) || fail 'Cannot list nodes'
  [[ -n $node_ids ]] || fail 'No Swarm nodes found'
  mapfile -t NODES <<< "$node_ids"
  (( ${#NODES[@]} >= 1 && ${#NODES[@]} <= 2 )) || fail 'This milestone supports one or two nodes'
  WORKER_ID=''; DB_NODE=''; local -a labels=()
  local expected_arch=''
  for node in "${NODES[@]}"; do
    state=$(docker node inspect --format '{{.Status.State}} {{.Spec.Availability}} {{.Description.Platform.OS}}' "$node") || fail 'Cannot inspect node'
    [[ $state == 'ready active linux' ]] || fail 'Every node must be Ready, Active and Linux'
    arch=$(docker node inspect --format '{{.Description.Platform.Architecture}}' "$node")
    [[ -z $expected_arch || $arch == "$expected_arch" ]] || fail 'Mixed node architectures are not supported'
    expected_arch=$arch
    label=$(docker node inspect --format '{{index .Spec.Labels "agenthub.node"}}' "$node")
    [[ $label =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'Label every node with a distinct agenthub.node value'
    [[ " ${labels[*]} " != *" $label "* ]] || fail 'Duplicate agenthub.node labels'
    labels+=("$label")
    if [[ $(docker node inspect --format '{{index .Spec.Labels "agenthub.db"}}' "$node") == true ]]; then
      [[ -z $DB_NODE ]] || fail 'Exactly one node may have agenthub.db=true'
      DB_NODE=$node
    fi
    if [[ $node != "$MANAGER_ID" ]]; then
      [[ $(docker node inspect --format '{{.Spec.Role}}' "$node") == worker ]] || fail 'This milestone supports one manager and one worker'
      WORKER_ID=$node
    fi
  done
  [[ -n $DB_NODE ]] || fail 'Missing agenthub.db=true node'
  if [[ -n $WORKER_ID ]]; then
    need ssh
    [[ ${WORKER_SSH:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@:-]*$ ]] || fail 'Set WORKER_SSH to a trusted SSH host alias or user@host'
    remote_id=$(on_node "$WORKER_ID" docker info --format '{{.Swarm.NodeID}}') || fail 'Worker SSH/Docker check failed'
    [[ $remote_id == "$WORKER_ID" ]] || fail 'WORKER_SSH reaches the wrong Docker node'
  fi
  if [[ $IMAGE_MODE == local || $STORAGE_MODE == directory ]]; then
    (( ${#NODES[@]} == 1 )) || fail 'Local images and directory-only storage are restricted to a one-node rehearsal'
  fi
  docker secret inspect "$DB_PASSWORD_SECRET" "$BOOTSTRAP_PASSWORD_SECRET" >/dev/null || fail 'Required Swarm secret is missing'
  # These are read-only checks. The operator must mount storage and pull images first.
  on_node "$DB_NODE" bash -s -- "$POSTGRES_DATA_PATH" "$STORAGE_MODE" "${POSTGRES_VOLUME_MOUNT:-}" "${POSTGRES_VOLUME_UUID:-}" <<'STORAGE' || fail 'Database storage check failed on the labeled node'
set -euo pipefail
data=$1; mode=$2; mount=$3; uuid=$4
[[ -d $data && $(readlink -f -- "$data") == "$data" ]]
if [[ $mode == volume ]]; then
  [[ $(readlink -f -- "$mount") == "$mount" ]]
  mountpoint -q -- "$mount"
  [[ $(findmnt -n -o UUID --target "$data") == "$uuid" ]]
  [[ $(findmnt -n -o TARGET --target "$data") == "$mount" ]]
  options=$(findmnt -n -o OPTIONS --target "$data")
  [[ ,$options, == *,rw,* ]]
fi
if [[ -e $data/PG_VERSION ]]; then
  [[ $(cat "$data/PG_VERSION") == 17 ]]
else
  contents=$(find "$data" -mindepth 1 -maxdepth 1 -print -quit)
  [[ -z $contents ]]
fi
STORAGE
  if [[ $IMAGE_MODE == registry ]]; then
    for image in "$AGENTHUB_IMAGE" "$POSTGRES_IMAGE"; do
      docker manifest inspect "$image" >/dev/null 2>&1 || fail 'Registry manifest lookup failed; verify image references and manager registry login'
    done
  fi
  for node in "${NODES[@]}"; do
    images=$(on_node "$node" docker image inspect "$AGENTHUB_IMAGE" "$POSTGRES_IMAGE" 2>/dev/null) || fail 'Selected images must already be pulled on every node'
    printf '%s' "$images" | python3 -c '
import json,sys
images=json.load(sys.stdin)
arch={"x86_64":"amd64","aarch64":"arm64"}.get(sys.argv[1],sys.argv[1])
if len(images)!=2 or not all(i["Os"]=="linux" and i["Architecture"]==arch for i in images):
    sys.exit("Image platform mismatch")
if "PG_MAJOR=17" not in images[1]["Config"].get("Env",[]):
    sys.exit("Expected the official PostgreSQL 17 image")
' "$expected_arch" || fail 'Images must match node architecture and PostgreSQL major version 17'
  done
}

check_existing() {
  local services service spec_data setup tasks node
  services=$(docker service ls --format '{{.Name}}') || fail 'Cannot list services'
  if grep -Fxq "${STACK}_setup" <<< "$services"; then
    setup=$(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup")
    [[ $setup == 0 ]] && stopped "${STACK}_setup" || fail 'Existing setup is armed or unfinished; inspect it and explicitly return it to zero before retrying'
  fi
  for service in $services; do
    spec_data=$(docker service inspect --format '{{json .Spec}}' "$service") || fail 'Cannot inspect existing service'
    printf '%s' "$spec_data" | python3 -c '
import json,os,sys
from pathlib import PurePosixPath
def require(condition):
    if not condition:
        sys.exit("Existing database settings or stack ownership differ; separate review required")
s=json.load(sys.stdin); name=s["Name"]; target=os.environ["STACK"]+"_db"
for port in s.get("EndpointSpec",{}).get("Ports",[]) or []:
    if port.get("PublishedPort")==int(os.environ["API_PUBLISHED_PORT"]) and port.get("Protocol","tcp")=="tcp" and name!=os.environ["STACK"]+"_api":
        sys.exit("Another service uses the requested API published port")
mounts=s["TaskTemplate"]["ContainerSpec"].get("Mounts",[])
path=PurePosixPath(os.environ["POSTGRES_DATA_PATH"])
for m in mounts:
    if m["Type"]=="bind":
        other=PurePosixPath(m["Source"])
        if (other==path or other in path.parents or path in other.parents) and name!=target:
            sys.exit("Another service overlaps the database directory")
if name==target:
    c=s["TaskTemplate"]["ContainerSpec"]
    env=dict(v.split("=",1) for v in c.get("Env",[]))
    require(s.get("Labels",{}).get("com.docker.stack.namespace")==os.environ["STACK"])
    require(c["Image"]==os.environ["POSTGRES_IMAGE"])
    require(env.get("POSTGRES_DB")==os.environ["POSTGRES_DB"] and env.get("POSTGRES_USER")==os.environ["POSTGRES_USER"])
    require(any(m["Type"]=="bind" and m["Source"]==str(path) and m["Target"]=="/var/lib/postgresql/data" for m in mounts))
    require([x["SecretName"] for x in c.get("Secrets",[])]==[os.environ["DB_PASSWORD_SECRET"]])
    require(not s.get("EndpointSpec",{}).get("Ports"))
    require(s["Mode"]["Replicated"]["Replicas"]==1)
elif name in [os.environ["STACK"]+"_"+n for n in ("api","worker","setup")]:
    require(s.get("Labels",{}).get("com.docker.stack.namespace")==os.environ["STACK"])
' || fail 'Existing services conflict with this deployment; no mutation performed'
  done
  # Include standalone/stopped containers: a later restart must not share PGDATA.
  local containers container_json
  containers=$(on_node "$DB_NODE" docker ps -aq) || fail 'Cannot inspect database-node containers'
  for service in $containers; do
    container_json=$(on_node "$DB_NODE" docker inspect "$service") || fail 'Cannot inspect database-node container'
    printf '%s' "$container_json" | python3 -c '
import json,os,sys
from pathlib import PurePosixPath
c=json.load(sys.stdin)[0]
if c.get("Config",{}).get("Labels",{}).get("com.docker.swarm.service.name")==os.environ["STACK"]+"_db":
    sys.exit(0)
path=PurePosixPath(os.environ["POSTGRES_DATA_PATH"])
for m in c.get("Mounts",[]):
    if m["Type"]=="bind":
        other=PurePosixPath(m["Source"])
        if other==path or other in path.parents or path in other.parents:
            sys.exit("Another container overlaps the database directory")
' || fail 'An existing container conflicts with database storage'
  done
  if grep -Fxq "${STACK}_db" <<< "$services"; then
    tasks=$(docker service ps -q --filter desired-state=running "${STACK}_db") || fail 'Cannot inspect database placement'
    for service in $tasks; do
      node=$(docker inspect --type task --format '{{.NodeID}}' "$service")
      [[ $node == "$DB_NODE" ]] || fail 'Existing database placement differs from the labeled node'
    done
  fi
}

online_preflight() {
  inspect_cluster
  check_existing
  note 'Preflight passed: manager, nodes, secrets, storage, cached images and existing database configuration checked.'
}
