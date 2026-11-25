#!/usr/bin/env bash
set -Eeuo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/common.sh"

approved_image() {
  [[ ${1%@*} == "$IMAGE_REPOSITORY" && ${1##*@} =~ ^sha256:[0-9a-f]{64}$ ]]
}

save_release() {
  local tmp
  tmp=$(mktemp "$state_file.XXXXXX")
  printf '%s %s %s\n' "$1" "$previous_image" "$candidate" > "$tmp"
  mv -fT -- "$tmp" "$state_file"
}

release_preflight() {
  inspect_cluster pull-application
  [[ ${#NODES[@]} == 2 && $DB_NODE == "$MANAGER_ID" ]] || fail 'Expected two nodes with the database on the manager'
  check_existing
  [[ $(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "${STACK}_setup") == 0 ]] ||
    fail 'Delivery requires a dormant setup service'
}

# Reconcile provider settings on existing services; image updates alone do not apply YAML.
# 对现有服务同步模型配置；仅更新镜像不会应用 YAML 中的设置。
provider_update_args() {
  local role=$1 service=$2 mounted secret command
  update_args+=(--env-add "REPLY_MODE=${REPLY_MODE:-fixed}")
  if [[ $role == api ]]; then
    update_args+=(--env-add SESSION_COOKIE_SECURE=true)
    return
  fi
  mounted=$(docker service inspect --format '{{range .Spec.TaskTemplate.ContainerSpec.Secrets}}{{if eq .File.Name "openai_api_key"}}{{.SecretName}}{{"\n"}}{{end}}{{end}}' "$service") || fail 'Cannot inspect worker secret mounts'
  while IFS= read -r secret; do
    [[ -n $secret ]] || continue
    update_args+=(--secret-rm "$secret")
  done <<< "$mounted"
  command='export PGPASSWORD="$(cat /run/secrets/db_password)"; test -n "$PGPASSWORD"; '
  update_args+=(--env-rm OPENAI_API_KEY)
  if [[ ${REPLY_MODE:-fixed} == openai ]]; then
    update_args+=(--secret-add "source=$OPENAI_API_KEY_SECRET,target=openai_api_key" --env-add "OPENAI_MODEL=$OPENAI_MODEL")
    command+='export OPENAI_API_KEY="$(cat /run/secrets/openai_api_key)"; test -n "$OPENAI_API_KEY"; '
  else
    update_args+=(--env-rm OPENAI_MODEL)
  fi
  command+='exec "$@"'
  update_args+=(--entrypoint "/bin/sh -ec '$command' --")
}

rollout() {
  local action=$1 state_file=$2 previous_image='' candidate='' phase='' extra=''
  local role replicas service image status initial='' config_only=false
  local -a update_args
  if [[ -e $state_file ]]; then
    read -r phase previous_image candidate extra < "$state_file"
    [[ $phase =~ ^(migrating|pending|deployed|reverted)$ && -z $extra ]] &&
      approved_image "$previous_image" && approved_image "$candidate" || fail 'Invalid release record'
  fi
  if [[ $action == rollback ]]; then
    [[ $phase == pending || $phase == deployed ]] && [[ $AGENTHUB_IMAGE == "$previous_image" ]] ||
      fail 'Rollback requires the previous digest in the current release record'
  else
    [[ $phase != pending && $phase != migrating ]] || fail 'An interrupted release needs recovery before another deployment'
  fi

  release_preflight
  wait_healthy "${STACK}_db" 1 "$DB_NODE"
  for role in api worker; do
    service="${STACK}_$role"
    replicas=$API_REPLICAS
    [[ $role == api ]] || replicas=$WORKER_REPLICAS
    [[ $(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "$service") == "$replicas" ]] ||
      fail "Unexpected replica count for $service"
    image=$(docker service inspect --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}' "$service")
    approved_image "$image" || fail "Unrecognized current image for $service"
    if [[ $action == deploy ]]; then
      [[ -z $initial || $image == "$initial" ]] || fail 'Current application digests differ'
      initial=$image
      wait_healthy "$service" "$replicas" '' "$initial" baseline
    else
      [[ $image == "$candidate" || $image == "$previous_image" ]] || fail 'Service drift since deployment'
    fi
  done

  if [[ $action == deploy ]]; then
    if [[ $AGENTHUB_IMAGE == "$initial" ]]; then
      config_only=true
    else
      previous_image=$initial
      candidate=$AGENTHUB_IMAGE
      save_release migrating
      run_setup "$candidate"
    fi
  fi
  # Persist both immutable targets before the first application mutation.
  # 首次更新应用前，持久记录新旧镜像摘要。
  if [[ $config_only == false ]]; then save_release pending; fi
  for role in api worker; do
    service="${STACK}_$role"
    replicas=$API_REPLICAS
    [[ $role == api ]] || replicas=$WORKER_REPLICAS
    update_args=()
    provider_update_args "$role" "$service"
    if [[ $action == rollback ]]; then
      status=$(docker service inspect --format '{{if .UpdateStatus}}{{.UpdateStatus.State}}{{end}}' "$service")
      if [[ $status == paused ]]; then update_args+=(--force); fi
    fi
    docker service update --detach=true --with-registry-auth --image "$AGENTHUB_IMAGE" "${update_args[@]}" \
      --update-order start-first --update-parallelism 1 --update-delay 10s \
      --update-monitor 150s --update-max-failure-ratio 0 --update-failure-action pause "$service"
    wait_healthy "$service" "$replicas" '' "$AGENTHUB_IMAGE"
  done
  python3 "$SCRIPT_DIR/monitor.py" --stack "$STACK" --expected-nodes 2 \
    --url "http://127.0.0.1:$API_PUBLISHED_PORT" --api-replicas "$API_REPLICAS" --worker-replicas "$WORKER_REPLICAS" --reply-mode "${REPLY_MODE:-fixed}"
  if [[ $config_only == false ]]; then
    if [[ $action == deploy ]]; then save_release deployed; else save_release reverted; fi
  fi
  note "Healthy application digest: $AGENTHUB_IMAGE"
}

delivery_main() {
  local request='^(deploy|rollback) (sha256:[0-9a-f]{64})$'
  [[ ${SSH_ORIGINAL_COMMAND:-} =~ $request ]] || fail 'Expected deploy or rollback and one immutable digest'
  local action=${BASH_REMATCH[1]} digest=${BASH_REMATCH[2]}
  source /etc/agenthub-delivery/config
  export WORKER_SSH
  [[ $IMAGE_REPOSITORY =~ ^ghcr\.io/[a-z0-9_.-]+/[a-z0-9_.-]+/backend$ ]] || fail 'Invalid GHCR repository'
  export AGENTHUB_IMAGE="$IMAGE_REPOSITORY@$digest"
  configure
  [[ $IMAGE_MODE == registry && $STORAGE_MODE == volume ]] || fail 'Delivery requires registry images and Volume storage'
  umask 077
  exec 9>>"$DEPLOY_LOCK_PATH"
  flock -n 9 || fail 'Another backup or deployment holds the manager lock'
  rollout "$action" "/var/lib/agenthub-delivery/$STACK.release"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then delivery_main; fi
