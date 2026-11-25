#!/usr/bin/env bash
# Stage the stack, run setup once, and bring up the requested application replicas.
# 分阶段部署 stack，执行一次 setup，再启动所需的应用副本。
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

# Default to printing the plan. Only apply mode can change services.
# 默认打印部署计划。只有 apply 模式会变更服务。
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
# 此管理节点上的所有操作者都须使用同一锁文件，即使从不同的检出目录运行。
exec 9>"$DEPLOY_LOCK_PATH" || fail 'Cannot open deployment lock; arrange manager permissions first'
flock -n 9 || fail 'Another deployment holds the manager lock'
stage=preflight
# Check the live environment under the lock before changing any service.
# 在持有部署锁的情况下检查运行环境，然后才变更服务。
trap 'printf "Deployment stopped during %s. Inspect service/task state before retrying; no automatic recovery performed.\n" "$stage" >&2' ERR
online_preflight

# Save the requested counts before staging the stack with zero application replicas.
# 在以零应用副本部署 stack 之前，保存请求的副本数。
target_api=$API_REPLICAS
target_worker=$WORKER_REPLICAS
stage=database-startup
# Quiesce API and worker tasks before running migrations.
# 执行数据库迁移之前，先停止 API 和 worker 任务。
API_REPLICAS=0 WORKER_REPLICAS=0 docker stack deploy --detach=true --resolve-image never --with-registry-auth "${STACK_CONFIG_ARGS[@]}" "$STACK"
wait_stopped "${STACK}_api"
wait_stopped "${STACK}_worker"
wait_stopped "${STACK}_setup"
# Begin setup only after PostgreSQL is healthy and the old tasks are stopped.
# 等待旧任务停止且 PostgreSQL 健康后，再开始 setup。
wait_healthy "${STACK}_db" 1 "$DB_NODE"

stage=migration-and-bootstrap
run_setup

stage=application-startup
# Restore the requested replica counts after setup exits successfully.
# setup 成功退出后恢复所请求的副本数。
docker service scale --detach=true "${STACK}_api=$target_api" "${STACK}_worker=$target_worker"
wait_healthy "${STACK}_api" "$target_api"
wait_healthy "${STACK}_worker" "$target_worker"
note 'Deployment converged. Run the authenticated deployment smoke check separately.'
