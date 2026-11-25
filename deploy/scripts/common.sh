#!/usr/bin/env bash
# Shared checks for the two-node deployment scripts. No actions run on source.
# 两节点部署脚本共用的检查函数。加载此文件时只定义函数。

# Stop the current script with a diagnostic on stderr.
# 将诊断信息写入标准错误，并终止当前脚本。
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
# Write progress and plan output to stdout.
# 将进度和执行计划写入标准输出。
note() { printf '%s\n' "$*"; }
# Check command availability without invoking the command.
# 检查命令是否可用，不执行命令本身。
need() { command -v "$1" >/dev/null || fail "Required command missing: $1"; }

# Read and validate settings shared by the plan, preflight, and apply commands.
# 读取并验证计划、预检和部署命令共用的设置。
configure() {
  local name value
  # Export required settings for Docker interpolation and the Python checks.
  # 导出必填设置，供 Docker 插值和 Python 检查使用。
  for name in STACK AGENTHUB_IMAGE POSTGRES_IMAGE POSTGRES_DATA_PATH DB_PASSWORD_SECRET BOOTSTRAP_PASSWORD_SECRET POSTGRES_DB POSTGRES_USER BOOTSTRAP_USERNAME; do
    [[ -n ${!name:-} ]] || fail "Export $name; see deploy/swarm/config.example.env"
    export "$name"
  done
  # Default to registry images, mounted storage, and two replicas per application service.
  # 默认使用仓库镜像和已挂载的存储，两个应用服务各运行两个副本。
  export API_REPLICAS=${API_REPLICAS:-2} WORKER_REPLICAS=${WORKER_REPLICAS:-2}
  export API_PUBLISHED_PORT=${API_PUBLISHED_PORT:-18000}
  export IMAGE_MODE=${IMAGE_MODE:-registry} STORAGE_MODE=${STORAGE_MODE:-volume}
  export DEPLOY_TIMEOUT=${DEPLOY_TIMEOUT:-300}
  export DEPLOY_LOCK_PATH=${DEPLOY_LOCK_PATH:-/var/lock/agenthub-deploy.lock}
  export REPLY_MODE=${REPLY_MODE:-fixed} OPENAI_MODEL=${OPENAI_MODEL:-gpt-5.4-mini}
  [[ $REPLY_MODE == fixed || $REPLY_MODE == openai ]] || fail 'REPLY_MODE must be fixed or openai'
  if [[ $REPLY_MODE == openai ]]; then
    [[ ${OPENAI_API_KEY_SECRET:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'Set OPENAI_API_KEY_SECRET to a Swarm secret name'
    [[ $OPENAI_API_KEY_SECRET != "$DB_PASSWORD_SECRET" && $OPENAI_API_KEY_SECRET != "$BOOTSTRAP_PASSWORD_SECRET" ]] || fail 'Use an independent OpenAI secret'
    [[ $OPENAI_MODEL =~ ^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}$ ]] || fail 'Invalid OPENAI_MODEL'
    export OPENAI_API_KEY_SECRET
  fi
  # Validate the identifiers used in service names, secrets, and account settings.
  # 校验用于服务名、密钥和账号设置的标识符。
  for name in STACK DB_PASSWORD_SECRET BOOTSTRAP_PASSWORD_SECRET POSTGRES_DB POSTGRES_USER BOOTSTRAP_USERNAME; do
    value=${!name}
    [[ $value =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail "Invalid identifier in $name"
  done
  [[ $STACK =~ ^[a-z0-9][a-z0-9-]*$ ]] || fail 'STACK must use lowercase letters, digits and hyphens'
  [[ $DB_PASSWORD_SECRET != "$BOOTSTRAP_PASSWORD_SECRET" ]] || fail 'Use two independent secrets'
  # Validate replica counts, timeout, port, and the Linux paths used by deployment.
  # 校验副本数、超时、端口和部署使用的 Linux 路径。
  for name in API_REPLICAS WORKER_REPLICAS DEPLOY_TIMEOUT; do
    [[ ${!name} =~ ^[1-9][0-9]{0,3}$ ]] || fail "$name must be a positive integer (maximum 9999)"
  done
  [[ $API_PUBLISHED_PORT =~ ^[1-9][0-9]{0,4}$ ]] && (( API_PUBLISHED_PORT <= 65535 )) || fail 'Invalid API_PUBLISHED_PORT'
  for name in POSTGRES_DATA_PATH DEPLOY_LOCK_PATH; do
    [[ ${!name} =~ ^/[a-zA-Z0-9_./-]+$ && ${!name} != / && ${!name}/ != *'/../'* ]] || fail "$name must be an absolute path without spaces or parent traversal"
  done
  # Registry releases require pinned digests. Local rehearsals accept versioned tags.
  # 仓库发布要求固定摘要。本地演练也可以使用带版本的标签。
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
  # Check the volume settings here and verify the actual filesystem during online preflight.
  # 此处校验数据盘设置，在线预检时再核对实际文件系统。
  case $STORAGE_MODE in
    volume)
      [[ ${POSTGRES_VOLUME_MOUNT:-} =~ ^/[a-zA-Z0-9_./-]+$ && ${POSTGRES_VOLUME_MOUNT:-} != / ]] || fail 'Set POSTGRES_VOLUME_MOUNT to the Volume mount point'
      [[ ${POSTGRES_VOLUME_UUID:-} =~ ^[a-zA-Z0-9-]+$ ]] || fail 'Set POSTGRES_VOLUME_UUID to the filesystem UUID'
      [[ $POSTGRES_DATA_PATH == "$POSTGRES_VOLUME_MOUNT/"* ]] || fail 'Data directory must be below the Volume mount point'
      ;;
    directory) ;;
    *) fail 'STORAGE_MODE must be volume or directory' ;;
  esac
  need docker; need python3; need timeout
  SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  STACK_FILE="$SCRIPT_DIR/../swarm/stack.yaml"
  STACK_CONFIG_ARGS=(-c "$STACK_FILE")
  if [[ $REPLY_MODE == openai ]]; then STACK_CONFIG_ARGS+=(-c "$SCRIPT_DIR/../swarm/openai.yaml"); fi
  # Never print rendered configuration or feed it through interpolation twice.
  # 隐藏解析后的配置，避免将解析结果再次传入部署命令并重复插值。
  docker stack config "${STACK_CONFIG_ARGS[@]}" >/dev/null || fail 'Stack configuration is invalid'
}

# Run a command locally for the manager or over SSH for the registered worker.
# 管理节点上的命令在本机执行，已登记的工作节点上的命令通过 SSH 执行。
on_node() {
  local node=$1 quoted
  shift
  if [[ $node == "$MANAGER_ID" ]]; then
    "$@"
  else
    [[ $node == "$WORKER_ID" && -n ${WORKER_SSH:-} ]] || fail 'Unknown remote node'
    printf -v quoted '%q ' "$@"
    # Preserve argument boundaries when the remote Bash shell parses the command.
    # 远端 Bash 解析命令时保留各个参数的边界。
    ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$WORKER_SSH" "$quoted"
  fi
}

# Inspect every task in the service history, including tasks that are shutting down.
# 检查服务历史中的所有任务，包括正在停止的任务。
task_states() {
  local ids deadline=${2:-$((SECONDS + DEPLOY_TIMEOUT))}
  ids=$(probe_before_deadline "$deadline" docker service ps --no-trunc -q "$1") || return 1
  [[ -n $ids ]] || return 0
  local -a tasks
  mapfile -t tasks <<< "$ids"
  probe_before_deadline "$deadline" docker inspect --type task --format '{{.Status.State}}' "${tasks[@]}"
}

# Accept an empty service or a service whose tasks have all reached a recognized terminal state.
# 服务没有任务，或所有任务都进入受认可的终态时，判定该服务已停止。
stopped() {
  local states state
  states=$(task_states "$@") || return 1
  while IFS= read -r state; do
    case $state in ''|complete|shutdown|failed|rejected) ;; *) return 1 ;; esac
  done <<< "$states"
}

# Bound shutdown queries by one deadline and reject results received after it.
# 用同一截止时间限制停止状态查询，并拒绝截止后才收到的结果。
wait_stopped() {
  local service=$1 deadline=$((SECONDS + DEPLOY_TIMEOUT)) remaining
  while (( SECONDS < deadline )); do
    if stopped "$service" "$deadline"; then
      (( SECONDS < deadline )) && return 0
      break
    fi
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || break
    sleep "$((remaining < 2 ? remaining : 2))"
  done
  fail "Tasks did not stop: $service. Resolve unfinished/orphaned tasks before retrying."
}

# Apply the remaining time budget to one command, with a short grace period before forced termination.
# 用剩余时间限制单条命令，并在强制终止前留出短暂的宽限期。
probe_before_deadline() {
  local deadline=$1 remaining
  shift
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || return 124
  timeout --signal=TERM --kill-after=1s "${remaining}s" "$@"
}

# Give each read at most ten seconds of the service wait, including remote inspection.
# 每次只读查询最多使用服务等待预算中的十秒，远端检查也受此限制。
health_probe() {
  local deadline=$1 limit=$((SECONDS + 10))
  shift
  probe_before_deadline "$((deadline < limit ? deadline : limit))" "$@" </dev/null 2>/dev/null
}

# Check one fresh snapshot. On retry, detail tells wait_healthy what was missing.
# 每轮重新检查。需要重试时，通过 detail 告知 wait_healthy 尚未满足的条件。
service_healthy() {
  local service=$1 expected=$2 deadline=$3 required_node=$4 validation=${6:-}
  local update ids id record node state container health quoted observed_image
  local -a tasks=() inspect
  local -A seen=()

  detail='service inspection unavailable'
  update=$(health_probe "$deadline" docker service inspect --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' "$service") || return 1
  case $update in
    ''|completed) ;;
    failed|paused|rollback*)
      [[ $validation == baseline && $update == rollback_completed ]] ||
        fail "Update failed for $service ($update); inspect task history privately" ;;
    *) detail="update state is $update"; return 1 ;;
  esac

  detail='desired task list unavailable'
  ids=$(health_probe "$deadline" docker service ps --no-trunc -q --filter desired-state=running "$service") || return 1
  [[ -z $ids ]] || mapfile -t tasks <<< "$ids"
  detail="${#tasks[@]} desired tasks, expected $expected"
  (( ${#tasks[@]} == expected )) || return 1
  for id in "${tasks[@]}"; do
    detail='empty or duplicate task ID'
    [[ -n $id && -z ${seen[$id]:-} ]] || return 1
    seen[$id]=1
    detail="inspection unavailable for task $id"
    record=$(health_probe "$deadline" docker inspect --type task --format '{{.NodeID}}|{{.Status.State}}|{{if .Status.ContainerStatus}}{{.Status.ContainerStatus.ContainerID}}{{end}}' "$id") || return 1
    IFS='|' read -r node state container <<< "$record"
    detail="task $id is ${state:-unknown} without a running container"
    [[ $state == running && -n $container ]] || return 1
    [[ -z $required_node || $node == "$required_node" ]] || fail "Task $id is on the wrong node for $service"
    if [[ -n ${5:-} ]]; then
      detail="task $id has not converged to the approved image"
      observed_image=$(health_probe "$deadline" docker inspect --type task --format '{{.Spec.ContainerSpec.Image}}' "$id") || return 1
      [[ $observed_image == "$5" ]] || return 1
    fi

    # Inspect the container through the Docker daemon on its assigned node.
    # 通过任务所在节点的 Docker daemon 检查容器。
    inspect=(docker inspect --format '{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
    if [[ $node != "$MANAGER_ID" ]]; then
      [[ -n $node && $node == "$WORKER_ID" && -n ${WORKER_SSH:-} ]] || fail 'Unknown remote node'
      printf -v quoted '%q ' "${inspect[@]}"
      inspect=(ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$WORKER_SSH" "$quoted")
    fi
    detail="container inspection unavailable for task $id on $node"
    health=$(health_probe "$deadline" "${inspect[@]}") || return 1
    detail="task $id on $node reports container state|health ${health:-unknown}"
    [[ $health == 'running|healthy' ]] || return 1
  done
}

# Retry read-only observations within one deadline; never reuse a partial count.
# 在同一截止时间内重试只读检查，每轮独立核对全部副本。
wait_healthy() {
  local service=$1 expected=$2 deadline=$((SECONDS + DEPLOY_TIMEOUT)) remaining
  local detail='no observation completed' previous_detail=''
  while (( SECONDS < deadline )); do
    if service_healthy "$service" "$expected" "$deadline" "${3:-}" "${4:-}" "${5:-}"; then
      (( SECONDS < deadline )) || { detail='healthy observations arrived too late'; break; }
      note "$service: $expected healthy tasks"
      return 0
    fi
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || break
    if [[ $detail != "$previous_detail" ]]; then
      printf 'Waiting for %s: %s\n' "$service" "$detail" >&2
      previous_detail=$detail
    fi
    sleep "$((remaining < 2 ? remaining : 2))"
  done
  fail "Health/convergence timeout for $service: $detail"
}

# Verify the manager, node placement, storage, and images required for deployment.
# 核对部署所需的管理节点、节点调度条件、存储和镜像。
inspect_cluster() {
  local endpoint node state label arch remote_id images image node_ids
  # Require a native manager so local filesystem checks refer to the daemon host.
  # 要求在原生管理节点上执行，以便本地文件系统检查对应 Docker daemon 所在的主机。
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
  # Require compatible nodes, distinct placement labels, and exactly one database node.
  # 要求各节点平台兼容、调度标签互不重复，并且只有一个数据库节点。
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
  # Match the SSH host to the worker node recorded by the manager.
  # 确认 SSH 实际连接的是管理节点记录的工作节点。
  if [[ -n $WORKER_ID ]]; then
    need ssh
    [[ ${WORKER_SSH:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_.@:-]*$ ]] || fail 'Set WORKER_SSH to a trusted SSH host alias or user@host'
    remote_id=$(on_node "$WORKER_ID" docker info --format '{{.Swarm.NodeID}}') || fail 'Worker SSH/Docker check failed'
    [[ $remote_id == "$WORKER_ID" ]] || fail 'WORKER_SSH reaches the wrong Docker node'
  fi
  # The rehearsal exceptions only apply when all services run on one node.
  # 本地镜像和目录存储这两种演练模式只适用于单节点环境。
  if [[ $IMAGE_MODE == local || $STORAGE_MODE == directory ]]; then
    (( ${#NODES[@]} == 1 )) || fail 'Local images and directory-only storage are restricted to a one-node rehearsal'
  fi
  docker secret inspect "$DB_PASSWORD_SECRET" "$BOOTSTRAP_PASSWORD_SECRET" >/dev/null || fail 'Required Swarm secret is missing'
  if [[ $REPLY_MODE == openai ]]; then
    docker secret inspect "$OPENAI_API_KEY_SECRET" >/dev/null || fail 'OpenAI Swarm secret is missing'
  fi
  # These are read-only checks. The operator must mount storage and pull images first.
  # 这些检查只读。操作者须先挂载存储并拉取镜像。
  on_node "$DB_NODE" bash -s -- "$POSTGRES_DATA_PATH" "$STORAGE_MODE" "${POSTGRES_VOLUME_MOUNT:-}" "${POSTGRES_VOLUME_UUID:-}" <<'STORAGE' || fail 'Database storage check failed on the labeled node'
set -euo pipefail
data=$1; mode=$2; mount=$3; uuid=$4
# Require a real, canonical directory on the database node.
# 要求数据库节点上的目录真实存在，且传入路径与规范路径一致。
[[ -d $data && $(readlink -f -- "$data") == "$data" ]]
if [[ $mode == volume ]]; then
  # Confirm the data directory sits on the exact writable filesystem that was configured.
  # 确认数据目录位于配置指定的可写文件系统上。
  [[ $(readlink -f -- "$mount") == "$mount" ]]
  mountpoint -q -- "$mount"
  [[ $(findmnt -n -o UUID --target "$data") == "$uuid" ]]
  [[ $(findmnt -n -o TARGET --target "$data") == "$mount" ]]
  options=$(findmnt -n -o OPTIONS --target "$data")
  [[ ,$options, == *,rw,* ]]
fi
# Reuse a PostgreSQL 17 directory or require an empty directory for initialization.
# 复用 PostgreSQL 17 的数据目录，或者要求初始化目录为空。
if [[ -e $data/PG_VERSION ]]; then
  [[ $(cat "$data/PG_VERSION") == 17 ]]
else
  contents=$(find "$data" -mindepth 1 -maxdepth 1 -print -quit)
  [[ -z $contents ]]
fi
STORAGE
  # Check registry access separately from the local images available on each node.
  # 分别检查镜像仓库能否访问，以及每个节点是否已有本地镜像。
  if [[ $IMAGE_MODE == registry ]]; then
    for image in "$AGENTHUB_IMAGE" "$POSTGRES_IMAGE"; do
      docker manifest inspect "$image" >/dev/null 2>&1 || fail 'Registry manifest lookup failed; verify image references and manager registry login'
    done
  fi
  for node in "${NODES[@]}"; do
    if [[ ${1:-} == pull-application ]]; then
      on_node "$node" docker pull "$AGENTHUB_IMAGE" >/dev/null || fail 'Application image pull failed'
    fi
    images=$(on_node "$node" docker image inspect "$AGENTHUB_IMAGE" "$POSTGRES_IMAGE" 2>/dev/null) || fail 'Selected images must already be pulled on every node'
    printf '%s' "$images" | python3 -c '
import json,sys
images=json.load(sys.stdin)
# Normalize Docker node architecture names before comparing them with the image metadata.
# 统一 Docker 节点架构名称，再与镜像元数据做比较。
arch={"x86_64":"amd64","aarch64":"arm64"}.get(sys.argv[1],sys.argv[1])
if len(images)!=2 or not all(i["Os"]=="linux" and i["Architecture"]==arch for i in images):
    sys.exit("Image platform mismatch")
if "PG_MAJOR=17" not in images[1]["Config"].get("Env",[]):
    sys.exit("Expected the official PostgreSQL 17 image")
' "$expected_arch" || fail 'Images must match node architecture and PostgreSQL major version 17'
  done
}

# Check for conflicting services, mounts, or unfinished setup before applying changes.
# 应用变更前检查服务冲突、挂载冲突以及尚未结束的 setup。
check_existing() {
  local services service spec_data setup tasks node
  services=$(docker service ls --format '{{.Name}}') || fail 'Cannot list services'
  # Require the previous setup to be both scaled down and fully stopped.
  # 要求之前的 setup 副本数已归零，并且所有任务都已停止。
  if grep -Fxq "${STACK}_setup" <<< "$services"; then
    setup=$(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup")
    [[ $setup == 0 ]] && stopped "${STACK}_setup" || fail 'Existing setup is armed or unfinished; inspect it and explicitly return it to zero before retrying'
  fi
  for service in $services; do
    # Compare the existing service spec with the requested storage and secret settings.
    # 将现有服务定义与本次部署要求的存储及密钥配置进行比对。
    spec_data=$(docker service inspect --format '{{json .Spec}}' "$service") || fail 'Cannot inspect existing service'
    printf '%s' "$spec_data" | python3 -c '
import json,os,re,sys
from pathlib import PurePosixPath
# Use a common diagnostic for settings that require an explicit migration decision.
# 对需要单独评估迁移的配置差异，使用统一的诊断信息。
def require(condition):
    if not condition:
        sys.exit("Existing database settings or stack ownership differ; separate review required")

# Normalize only digest-pinned references to the official Docker Hub postgres repository.
# 仅规范化通过摘要固定的 Docker Hub 官方 postgres 仓库引用。
def normalize_postgres_image(image):
    match = re.fullmatch(
        r"(?:(?:index\.)?docker\.io/)?(?:library/)?postgres"
        r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@(sha256:[0-9a-f]{64})", image
    )
    return "docker.io/library/postgres@" + match[1] if match else image

s=json.load(sys.stdin); name=s["Name"]; target=os.environ["STACK"]+"_db"
# Check the published port and reject any other service that uses the same TCP port.
# 检查发布端口，拒绝其他使用相同 TCP 端口的服务。
for port in s.get("EndpointSpec",{}).get("Ports",[]) or []:
    if port.get("PublishedPort")==int(os.environ["API_PUBLISHED_PORT"]) and port.get("Protocol","tcp")=="tcp" and name!=os.environ["STACK"]+"_api":
        sys.exit("Another service uses the requested API published port")
mounts=s["TaskTemplate"]["ContainerSpec"].get("Mounts",[])
path=PurePosixPath(os.environ["POSTGRES_DATA_PATH"])
# Treat a parent or child bind mount as overlapping the PostgreSQL directory.
# 将父目录或子目录的绑定挂载也视为与 PostgreSQL 数据目录重叠。
for m in mounts:
    if m["Type"]=="bind":
        other=PurePosixPath(m["Source"])
        if (other==path or other in path.parents or path in other.parents) and name!=target:
            sys.exit("Another service overlaps the database directory")
# Check invariant database settings and verify ownership of the stack service names.
# 核对数据库的固定配置，并确认同名服务归属本 stack。
if name==target:
    c=s["TaskTemplate"]["ContainerSpec"]
    env=dict(v.split("=",1) for v in c.get("Env",[]))
    require(s.get("Labels",{}).get("com.docker.stack.namespace")==os.environ["STACK"])
    require(normalize_postgres_image(c["Image"]) == normalize_postgres_image(os.environ["POSTGRES_IMAGE"]))
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
  # 同时检查独立容器及已停止的容器，防止它们重启后共用 PGDATA。
  local containers container_json
  containers=$(on_node "$DB_NODE" docker ps -aq) || fail 'Cannot inspect database-node containers'
  for service in $containers; do
    container_json=$(on_node "$DB_NODE" docker inspect "$service") || fail 'Cannot inspect database-node container'
    printf '%s' "$container_json" | python3 -c '
import json,os,sys
from pathlib import PurePosixPath
c=json.load(sys.stdin)[0]
# Exempt the stack database that owns this directory.
# 跳过本 stack 中使用该目录的数据库容器。
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
  # Confirm that an existing database task is still assigned to the labeled node.
  # 确认已有数据库任务仍然分配到带数据库标签的节点。
  if grep -Fxq "${STACK}_db" <<< "$services"; then
    tasks=$(docker service ps -q --filter desired-state=running "${STACK}_db") || fail 'Cannot inspect database placement'
    for service in $tasks; do
      node=$(docker inspect --type task --format '{{.NodeID}}' "$service")
      [[ $node == "$DB_NODE" ]] || fail 'Existing database placement differs from the labeled node'
    done
  fi
}

# Complete the read-only checks before any deployment work starts.
# 在执行部署变更前完成全部只读检查。
online_preflight() {
  inspect_cluster
  check_existing
  note 'Preflight passed: manager, nodes, secrets, storage, cached images and existing database configuration checked.'
}

run_setup() {
  local previous id deadline selected completed tasks state exit_code remaining
  local expected_image=${1:-} observed_image replicas restart_policy
  local -a launch=(docker service update --detach=true)
  if [[ -n $expected_image ]]; then launch+=(--image "$expected_image" --with-registry-auth); fi
  # Require a dormant setup service with retries disabled before launching one task.
  # 启动单次 setup 前，确认服务处于零副本且禁用了自动重启。
  replicas=$(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup") || fail 'Cannot inspect setup replica count'
  [[ $replicas == 0 ]] || fail 'Setup must be at zero before launch'
  restart_policy=$(docker service inspect --format '{{.Spec.TaskTemplate.RestartPolicy.Condition}}' "${STACK}_setup") || fail 'Cannot inspect setup restart policy'
  [[ $restart_policy == none ]] || fail 'Setup must not restart automatically'
  previous=$(docker service ps --no-trunc -q "${STACK}_setup")
  # Exclude historical tasks when verifying the new setup run.
  # 验证本次 setup 执行结果时排除历史任务。
  local -A seen=()
  while IFS= read -r id; do [[ -z $id ]] || seen["$id"]=1; done <<< "$previous"
  "${launch[@]}" --replicas 1 --force "${STACK}_setup"
  deadline=$((SECONDS + DEPLOY_TIMEOUT))
  selected=''; completed=false
  # Track one new task and use its terminal state and exit code to decide whether to proceed.
  # 跟踪本次新增的唯一任务，根据终态和退出码决定是否继续。
  while (( SECONDS < deadline )); do
    tasks=$(probe_before_deadline "$deadline" docker service ps --no-trunc -q "${STACK}_setup") || break
    while IFS= read -r id; do
      [[ -n $id && -z ${seen[$id]:-} ]] || continue
      [[ -z $selected || $selected == "$id" ]] || fail 'Multiple new setup tasks detected; stop for review'
      selected=$id
    done <<< "$tasks"
    if [[ -n $selected ]]; then
      state=$(probe_before_deadline "$deadline" docker inspect --type task --format '{{.Status.State}}' "$selected") || break
      case $state in
        complete)
          if [[ -n $expected_image ]]; then
            observed_image=$(probe_before_deadline "$deadline" docker inspect --type task --format '{{.Spec.ContainerSpec.Image}}' "$selected") || break
            [[ $observed_image == "$expected_image" ]] || fail 'Unexpected migration task image'
          fi
          exit_code=$(probe_before_deadline "$deadline" docker inspect --type task --format '{{.Status.ContainerStatus.ExitCode}}' "$selected") || break
          # A completed query must still arrive within the setup wait budget.
          # 即使查询已完成，结果仍必须在 setup 等待预算内返回。
          (( SECONDS < deadline )) || break
          [[ $exit_code == 0 ]] || fail 'Setup returned a nonzero exit code'
          completed=true; break ;;
        failed|rejected|orphaned|shutdown) fail "Setup ended unsuccessfully ($state)" ;;
      esac
    fi
    remaining=$((deadline - SECONDS))
    (( remaining > 0 )) || break
    sleep "$((remaining < 2 ? remaining : 2))"
  done
  [[ $completed == true ]] || fail 'Setup timed out; it may still be running. Do not restart it blindly.'
  # Return setup to its dormant state so it is ready for the next deployment.
  # 将 setup 恢复为零副本，为下次部署做好准备。
  docker service scale --detach=true "${STACK}_setup=0"
  wait_stopped "${STACK}_setup"
  # A replacement task may appear between the exit check and scale-down.
  # 检查退出码后到缩容完成前，可能出现新的替代任务。
  tasks=$(probe_before_deadline "$((SECONDS + DEPLOY_TIMEOUT))" docker service ps --no-trunc -q "${STACK}_setup") || fail 'Cannot verify final setup task history'
  while IFS= read -r id; do
    [[ -z $id || $id == "$selected" ]] && continue
    [[ -n ${seen[$id]:-} ]] || fail 'Multiple new setup tasks detected at scale-down; inspect migration state before retrying'
  done <<< "$tasks"
  note 'Alembic migration and bootstrap completed with exit code 0.'
}
