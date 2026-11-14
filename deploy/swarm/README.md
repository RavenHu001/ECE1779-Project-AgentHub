# AgentHub Swarm deployment — Milestone 1

For the Milestone 2 automation, image release workflow and proposed two-node
procedure, see [CLOUD_DEPLOYMENT.md](../../docs/CLOUD_DEPLOYMENT.md) and
[config.example.env](config.example.env). The manual commands below remain the
Docker Desktop rehearsal procedure; the new online automation requires a native
Linux manager. Existing test evidence remains in [LOCAL_VALIDATION.md](LOCAL_VALIDATION.md).

This stack reuses the existing backend runtime image for FastAPI, workers, and
database setup. It runs two API replicas, two worker replicas, and one PostgreSQL
17 replica. A fourth service, `setup`, normally has zero replicas and is started
manually to run migrations and bootstrap. All services use an overlay network;
`db` and `api` resolve through Swarm service discovery. API ingress uses Swarm's
routing mesh. The local `compose.yaml` remains the development setup; the steps
below describe the manual Milestone 1 procedure.

This milestone uses `REPLY_MODE=fixed` only. No model key is required or mounted.
HTTPS, automated delivery, DigitalOcean alerts, backups, and database failover are outside
this milestone. A read-only point-in-time monitor is documented in
[CLOUD_DEPLOYMENT.md](../../docs/CLOUD_DEPLOYMENT.md#read-only-monitoring). Commands below are Bash commands, run from the repository root
on a Linux Swarm manager unless another host is specified. Use a dedicated test
Swarm, not a shared development database, for the first rehearsal.

## Configuration and boundaries

| Variable | Meaning |
| --- | --- |
| `STACK` | Shell variable used in these commands; example `agenthub-m1` |
| `AGENTHUB_IMAGE` | Required registry image with a unique version tag or digest; identical for API, worker, and setup |
| `POSTGRES_IMAGE` | Required PostgreSQL 17 image tag or digest; do not upgrade major versions against existing data |
| `POSTGRES_DATA_PATH` | Required absolute directory on the database host, beneath the mounted DigitalOcean Volume |
| `DB_PASSWORD_SECRET` | Required name of an existing Swarm secret |
| `BOOTSTRAP_PASSWORD_SECRET` | Required name of an existing Swarm secret |
| `POSTGRES_DB`, `POSTGRES_USER` | Default `agenthub`; keep consistent with an initialized database |
| `BOOTSTRAP_USERNAME` | Default `developer`; bootstrap preserves existing account passwords |
| `API_PUBLISHED_PORT` | Default `18000`; published on every Swarm node |
| `API_REPLICAS`, `WORKER_REPLICAS` | Default `2`; set both to `0` during database setup |

`docker stack deploy` does not automatically load the repository's `.env` and
does not build images. Export the non-secret variables in the deploying shell.
The required-value checks reject missing settings, but cannot verify that a tag
is immutable or a directory is really on a DigitalOcean Volume.

The stack's small shell entrypoint reads `/run/secrets/db_password`, exports the
`PGPASSWORD` expected by the existing application, and uses `exec` to preserve
signal delivery. Only `setup` receives the bootstrap password. PostgreSQL uses
its image's `POSTGRES_PASSWORD_FILE` support. No application or Dockerfile change
is needed. Docker health checks run separately, so the worker probe also reads
the database secret itself. Secret contents are never interpolated into YAML.

The `$$` sequences in YAML become literal `$` characters in the container command
after one Swarm interpolation pass. Validate with `docker stack config`, but
deploy the original `stack.yaml`; do not pipe rendered output back into deployment
and accidentally interpolate the shell variables a second time.

Resource reservations are 128 MiB per application task and 256 MiB for PostgreSQL;
limits are 512 MiB and 1 GiB respectively. These are starting values, not measured
capacity guarantees. Allow room for the OS, Docker, and extra tasks during
start-first application updates. PostgreSQL updates are stop-first to avoid
concurrent writers to its data directory. Failed updates pause for inspection.

Application placement prefers distinct `agenthub.node` label values. This is a
preference, not a hard one-replica-per-node limit: a single node can host all
replicas. PostgreSQL requires `agenthub.db=true` on exactly one node. Do not label
a replacement database node until storage recovery has been completed.

## 1. Prepare nodes and storage

For a two-node DigitalOcean deployment, prepare two Linux Droplets in the same
region/VPC with compatible Docker Engines and the same CPU architecture. Use one
manager and one worker node; allow the manager to run application tasks. Permit
TCP 2377 to the manager and TCP/UDP 7946 and UDP 4789 between the nodes' private
addresses only. Keep the database port unpublished.

On the manager, substituting its private address:

```bash
docker swarm init --advertise-addr MANAGER_PRIVATE_IP
docker swarm join-token worker
```

Run the displayed join command on the second node, using the private manager
address. Keep the join token private. Then label nodes on the manager:

```bash
docker node ls
docker node update --label-add agenthub.node=node-a --label-add agenthub.db=true NODE_A
docker node update --label-add agenthub.node=node-b NODE_B
```

On node A, attach and persistently mount the DigitalOcean Volume using the
provider's instructions. The example mount below must be replaced with the
actual mount point. Verify the expected device and filesystem before creating
the data directory:

```bash
export VOLUME_MOUNT=/mnt/agenthub-volume
mountpoint -q "$VOLUME_MOUNT" || { echo 'Expected Volume is not mounted'; exit 1; }
findmnt --target "$VOLUME_MOUNT"
sudo mkdir -p "$VOLUME_MOUNT/postgres"
```

Set `POSTGRES_DATA_PATH` to that directory when deploying. It must exist on the
Docker daemon's host, not merely on the machine running the Docker CLI. Use a
fresh directory for the first deployment. The PostgreSQL image initializes its
ownership; do not make the directory world-writable. A missing bind source fails
the task, but an existing directory on an unmounted filesystem can silently use
the host disk. Configure host boot ordering so Docker/database startup waits for
the Volume, and repeat the mount check after reboot. A bind mount alone cannot
enforce that requirement.

No automatic storage relocation or database failover is configured. A Volume is
not a backup. Losing the only manager prevents normal rescheduling and cluster
management, even if some containers on the other node continue running.

## 2. Publish and select the runtime image

Build from the repository root on a machine matching the Droplet architecture.
Replace `YOUR_ORG` with a registry namespace you control. Use a new tag for every
different build, including uncommitted changes; do not overwrite a release tag.

```bash
export AGENTHUB_IMAGE=ghcr.io/YOUR_ORG/agenthub:7008d9f-m1
docker login ghcr.io
docker build --target runtime -t "$AGENTHUB_IMAGE" backend
docker push "$AGENTHUB_IMAGE"
export POSTGRES_IMAGE=postgres:17-bookworm
docker pull "$POSTGRES_IMAGE"
docker image inspect --format '{{index .RepoDigests 0}}' "$POSTGRES_IMAGE"
```

Prefer setting both image variables to the published `repository@sha256:...`
references for reproducibility; the `17-bookworm` tag can change. On **each node**,
verify `docker pull` succeeds for both selected images. Authenticate privately
where necessary. Deployment uses `--with-registry-auth` to pass the deploying
manager client's registry credentials to service tasks. Never put registry
passwords in this file or in command arguments.

## 3. Create external secrets

Set the deployment variables on the manager. Keep these names separate from any
other environment's secrets and data:

```bash
export STACK=agenthub-m1
export POSTGRES_DATA_PATH=/mnt/agenthub-volume/postgres
export DB_PASSWORD_SECRET=agenthub_m1_db_password_v1
export BOOTSTRAP_PASSWORD_SECRET=agenthub_m1_bootstrap_password_v1
export POSTGRES_DB=agenthub
export POSTGRES_USER=agenthub
export BOOTSTRAP_USERNAME=developer
export API_PUBLISHED_PORT=18000
```

Create each secret once, entering independent nonempty passwords privately. The
bootstrap password must contain only ASCII characters, as required by the current
backend account command. Do not enable shell tracing (`set -x`). Save the bootstrap
login in your password manager. This function sends each value on standard input,
not in arguments:

```bash
create_secret() {
  local value
  read -r -s -p "Password for $1: " value
  printf '\n'
  if [ -z "$value" ]; then echo 'Password must not be empty'; return 1; fi
  printf '%s' "$value" | docker secret create "$1" -
}
create_secret "$DB_PASSWORD_SECRET"
create_secret "$BOOTSTRAP_PASSWORD_SECRET"
```

Do not recreate secrets on every deployment. Changing a secret does not change
an existing PostgreSQL role password or an existing application's user password.
Coordinate password rotation separately. Neither API nor worker receives the
bootstrap password in its long-running service.

## 4. Stage database setup explicitly

Swarm does not provide Compose's `depends_on` startup sequencing. **Do not deploy
the default two application replicas against an uninitialized database.** Only
one operator should run setup or deploy this environment at a time.

First deploy with applications stopped and `setup` at its default zero replicas:

```bash
export API_REPLICAS=0 WORKER_REPLICAS=0
docker stack config -c deploy/swarm/stack.yaml > /dev/null
docker stack deploy --with-registry-auth -c deploy/swarm/stack.yaml "$STACK"
docker service ps "${STACK}_db" --no-trunc
```

On the **database node**, wait for the container to exist and become healthy.
Set `STACK` there too if it is a different shell. This bounded check exits
nonzero on timeout; do not continue after a failure:

```bash
wait_for_database() {
  local container state
  for attempt in $(seq 1 90); do
    container=$(docker ps -q --filter "label=com.docker.swarm.service.name=${STACK}_db" | head -n 1)
    if [ -n "$container" ]; then
      state=$(docker inspect --format '{{.State.Health.Status}}' "$container")
      if [ "$state" = healthy ]; then return 0; fi
    fi
    sleep 2
  done
  echo 'Database did not become healthy within 180 seconds'
  return 1
}
wait_for_database
```

Back on the **manager**, define this helper to start one fresh task and require
successful completion. It requires zero desired replicas and no unfinished prior
tasks before launching, and excludes all prior task IDs from the result check.
Rerunning it after a timeout will refuse to force-replace the original task:

```bash
run_once_and_wait() {
  local service="$1" replicas previous tasks task state code selected=""
  replicas=$(docker service inspect --format '{{.Spec.Mode.Replicated.Replicas}}' "$service") || return 1
  if [ "$replicas" != 0 ]; then
    echo 'Refusing to start: first inspect the existing run; desired replicas must be zero'
    return 1
  fi
  previous=$(docker service ps -q --no-trunc "$service") || return 1
  for task in $previous; do
    state=$(docker inspect --type task --format '{{.Status.State}}' "$task") || return 1
    case "$state" in
      complete|failed|rejected|shutdown) ;;
      *) echo "Prior task is not confirmed stopped: $task ($state)"; return 1 ;;
    esac
  done
  docker service update --detach=true --replicas 1 --force "$service" || return 1
  for attempt in $(seq 1 150); do
    tasks=$(docker service ps -q --no-trunc "$service") || return 1
    for task in $tasks; do
      if printf '%s\n' "$previous" | grep -Fxq "$task"; then continue; fi
      if [ -n "$selected" ] && [ "$selected" != "$task" ]; then
        echo 'More than one new task appeared; inspect the service before proceeding'
        return 1
      fi
      selected="$task"
    done
    if [ -n "$selected" ]; then
      state=$(docker inspect --type task --format '{{.Status.State}}' "$selected") || return 1
      case "$state" in
        complete)
          code=$(docker inspect --type task --format '{{.Status.ContainerStatus.ExitCode}}' "$selected") || return 1
          if [ "$code" = 0 ]; then return 0; fi
          return 1 ;;
        failed|rejected|orphaned|shutdown)
          echo "Task ended unsuccessfully: $state"
          docker service ps --no-trunc "$service"
          return 1 ;;
      esac
    fi
    sleep 2
  done
  echo 'Task did not complete within 300 seconds; inspect it before retrying'
  return 1
}
run_once_and_wait "${STACK}_setup"
```

Proceed **only if this returns success**. The task runs `alembic upgrade head`,
then `python -m app.manage bootstrap`. It has no automatic restart. On failure,
leave applications at zero, inspect `docker service logs "${STACK}_setup"`
privately, correct the cause, and rerun the helper. On timeout, inspect whether
the original task is still running before starting another. Before an intentional
retry, confirm all prior tasks have stopped, run `docker service scale "${STACK}_setup=0"`,
then rerun the helper. A timeout only stops waiting; it does
not cancel the task. Do not scale down a migration that is still running merely
to bypass the guard. These checks are not a distributed lock: two operators can
still race, so keep deployments serialized and do not redeploy the stack while
setup runs. Resolve unreachable/orphaned tasks on their node before retrying.
Do not run migrations concurrently or edit applied migration files.

After successful setup, return setup to zero and start the application replicas:

```bash
docker service scale "${STACK}_setup=0"
export API_REPLICAS=2 WORKER_REPLICAS=2
docker stack deploy --with-registry-auth -c deploy/swarm/stack.yaml "$STACK"
docker stack services "$STACK"
docker service ps "${STACK}_api"
docker service ps "${STACK}_worker"
```

Expected steady state: `api` 2/2, `worker` 2/2, `db` 1/1, `setup` 0/0. Wait for
convergence and inspect task errors if replicas remain pending. `docker stack
deploy` returning successfully does not mean the deployment is healthy.

For a later image release, this milestone's conservative migration procedure is
to stop API/workers first with `docker service scale`, verify their old tasks
have stopped, select the new image, and repeat this section with replicas zero.
This introduces planned downtime. Rolling policies support application-only
updates after schema compatibility is confirmed by the backend owner; they do
not make schema migrations or PostgreSQL upgrades zero-downtime operations.

## 5. Validate the running deployment

On the manager:

```bash
curl --fail "http://127.0.0.1:${API_PUBLISHED_PORT}/health"
docker network inspect "${STACK}_backend" --format '{{.Driver}} {{.Scope}}'
docker service inspect "${STACK}_api" --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'
docker service inspect "${STACK}_worker" --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'
```

Expect fixed reply mode, an `overlay swarm` network, and matching API/worker image
versions. `/health` checks API/database connectivity, not schema completeness or
worker progress. The worker health check also only tests authenticated database
access. A hung processing loop can still pass that probe. Use the existing smoke
program to test actual jobs, ordering of the request/reply, and duplicate submission.

Run it as a temporary Swarm service, using the same image and narrowly scoped
secrets; standalone `docker run` cannot consume Swarm secrets. The network need
not be attachable. Ensure no service with this smoke name already exists:

```bash
docker service create --detach=true --with-registry-auth \
  --name "${STACK}_smoke" --replicas 0 --restart-condition none \
  --network "${STACK}_backend" \
  --secret "source=${BOOTSTRAP_PASSWORD_SECRET},target=bootstrap_password" \
  --env "BOOTSTRAP_USERNAME=${BOOTSTRAP_USERNAME}" \
  --entrypoint /bin/sh "$AGENTHUB_IMAGE" -ec \
  'export BOOTSTRAP_PASSWORD="$(cat /run/secrets/bootstrap_password)"; exec python -m app.smoke'
run_once_and_wait "${STACK}_smoke"
docker service logs "${STACK}_smoke"
```

Require exit code 0 and the single-reply success message. Save its conversation
UUID. Then remove only the temporary smoke service:

```bash
docker service rm "${STACK}_smoke"
```

To test persistence, run `docker service update --force "${STACK}_db"`, wait for
database health again, and repeat the smoke service with its final command changed
to `python -m app.smoke --verify-conversation YOUR_SAVED_UUID`. Use the recorded
UUID, not a new conversation. This temporarily interrupts database access and
can restart unhealthy app tasks. It replaces a container without deleting data.

For browser checks, keep port 18000 blocked from the public Internet and use an
SSH tunnel to a Droplet (adjust the port if configured differently):

```bash
ssh -N -L 18000:127.0.0.1:18000 your-user@your-manager
```

Open `http://127.0.0.1:18000/` locally and sign in with the bootstrap account.
Swarm's ingress port listens on every node; it is **not loopback-bound**. Restrict
it using the cloud firewall. Do not send HTTP Basic credentials over public HTTP.

For two-node evidence, inspect API and worker task placement, request `/health`
through each node, and send repeated new HTTP connections while inspecting API
task logs. Successful requests to both node addresses prove ingress reachability;
logs from different task IDs are needed to demonstrate backend distribution.
Swarm balances connections, so keep-alive traffic need not alternate replicas.
The application does not need sticky sessions for its PostgreSQL-backed history.

## Windows PowerShell configuration validation

From the repository root in Windows 11 PowerShell, run the following offline
validation. It uses placeholders, restores previous environment values afterward,
and suppresses the rendered configuration. Docker's CLI is required, but a running
Docker Desktop engine, registry access, real secrets, and cloud resources are not:

```powershell
& {
    $validationEnv = @{
        AGENTHUB_IMAGE = 'example.invalid/agenthub:7008d9f-validation'
        POSTGRES_IMAGE = 'postgres:17-bookworm'
        POSTGRES_DATA_PATH = '/mnt/agenthub-validation/postgres'
        DB_PASSWORD_SECRET = 'agenthub-validation-db'
        BOOTSTRAP_PASSWORD_SECRET = 'agenthub-validation-bootstrap'
        POSTGRES_DB = 'agenthub'
        POSTGRES_USER = 'agenthub'
        BOOTSTRAP_USERNAME = 'developer'
        API_PUBLISHED_PORT = '18000'
        API_REPLICAS = '2'
        WORKER_REPLICAS = '2'
    }
    $savedEnv = @{}
    foreach ($name in $validationEnv.Keys) {
        $savedEnv[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        foreach ($name in $validationEnv.Keys) {
            [Environment]::SetEnvironmentVariable($name, $validationEnv[$name], 'Process')
        }
        docker stack config -c deploy/swarm/stack.yaml > $null
        if ($LASTEXITCODE -ne 0) { throw 'Swarm configuration validation failed' }
        Write-Host 'Swarm configuration parsed successfully; nothing was deployed.'
    }
    finally {
        foreach ($name in $savedEnv.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnv[$name], 'Process')
        }
    }
}
```

The Linux data path is only a parser placeholder, not a Windows directory or a
validated Docker Desktop mount. For container tests, start Docker Desktop in
Linux-container mode and check `docker info --format '{{.OSType}}'`. The deployment
commands elsewhere in this guide use Bash, not PowerShell; use the Linux rehearsal
below for actual Swarm testing. Do not deploy the placeholder image or secret names.

## Local single-node Swarm rehearsal

Use a disposable Linux VM or a Linux Docker Engine in WSL with its own storage.
Docker Desktop's daemon runs in a VM: an arbitrary Windows or WSL directory is
not automatically a valid Linux daemon bind path. The procedure above assumes
the commands and data directory are on the Linux daemon host.

1. Check `docker info --format '{{.Swarm.LocalNodeState}}'`. If inactive, run
   `docker swarm init` (specify the test host's address if Docker requests it).
   Do not reinitialize an existing shared Swarm.
2. Label the local node:

   ```bash
   docker node update --label-add agenthub.node=local --label-add agenthub.db=true "$(docker info --format '{{.Swarm.NodeID}}')"
   ```

3. Use `STACK=agenthub-m1-local`, unique secret names, and a fresh host directory
   such as `/srv/agenthub-m1-local/postgres`, created with `sudo mkdir -p`. Set
   `POSTGRES_DATA_PATH` to it. This deliberately replaces the DigitalOcean mount
   step for the local rehearsal and does **not** validate cloud Volume mounting.
4. Prefer the registry image path above. For an offline registry-free rehearsal,
   build `docker build --target runtime -t agenthub-swarm-local:7008d9f backend`,
   set `AGENTHUB_IMAGE=agenthub-swarm-local:7008d9f`, and add `--resolve-image never`
   to both stack deploy commands and `--no-resolve-image` to smoke service creation.
   PostgreSQL's image must also be present locally. This local-image exception is
   for one node only and does not test registry distribution.
5. Follow secret creation, staged setup, deployment, smoke, and persistence steps.
   Both application services still run **two replicas on the single node**.
6. Check `/health` on `localhost:18000` and the browser locally. Restrict access
   on the test host's network since ingress is not bound only to localhost.

To stop a dedicated rehearsal, remove its temporary smoke service if present,
then run `docker stack rm "$STACK"` and wait for tasks to exit. Stack removal
retains the bind-mounted PostgreSQL data and external secrets. Do not delete the
data directory or remove secrets needed by another environment. Do not force a
node to leave a shared Swarm. Reusing this directory also requires the original
database credentials; a new secret value does not reset the database password.

## Validation limits and remaining two-node work

Static parsing can be run without a daemon using the required non-secret
variables and `docker stack config -c deploy/swarm/stack.yaml > /dev/null`.
Parsing alone does not check registry permissions, secret existence, labels,
bind mounts, firewall rules, available memory, or successful migration execution.

Before calling the DigitalOcean deployment verified, record:

- Two ready nodes, exactly one database label, distinct application placement
  labels, and matching Docker/platform support.
- Successful pulls of the selected image digests on both nodes.
- Volume attachment, filesystem identity, persistent mount and boot ordering,
  PostgreSQL permissions, and data surviving container replacement.
- Private inter-node connectivity, restricted ingress, and SSH tunnel access.
- Successful setup exit, 2/2 API and worker replicas, 1/1 database, actual task
  distribution, smoke success, and requests reaching multiple API tasks.
- Recovery of application tasks after a controlled worker-node interruption,
  with the manager/database still available. Do not claim manager or database HA.
- Resource usage sufficient for the baseline and temporary update tasks; adjust
  the initial reservations/limits based on measured results.

No cloud resources are created by this repository change. HTTPS, automatic
deployment, DigitalOcean Metrics/Alerts, backups, automated rollback, and live
model validation are later milestones. A read-only monitoring snapshot is
documented in [CLOUD_DEPLOYMENT.md](../../docs/CLOUD_DEPLOYMENT.md#read-only-monitoring).
Local Compose tests remain the backend regression workflow; this Swarm rehearsal
is a separate deployment smoke check, not a live provider test.

References: [Docker stack deployment](https://docs.docker.com/engine/swarm/stack-deploy/),
[Swarm secrets](https://docs.docker.com/engine/swarm/secrets/),
[service placement and updates](https://docs.docker.com/engine/swarm/services/),
[DigitalOcean Volume mounting](https://docs.digitalocean.com/products/volumes/how-to/mount/).
