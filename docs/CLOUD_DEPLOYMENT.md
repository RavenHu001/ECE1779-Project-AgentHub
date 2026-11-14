# Cloud deployment: Milestone 2

This is the reviewable deployment procedure, not evidence of a cloud deployment.
Milestone 1's actual Docker Desktop results are in
[LOCAL_VALIDATION.md](../deploy/swarm/LOCAL_VALIDATION.md). Milestone 2 adds scripts
and manual image publication; it does not provision resources or automatically
deploy a release. Keep the existing local validation stack unchanged during review.

## Architecture and prerequisites

Use two Linux DigitalOcean Droplets in the same region and VPC: one Swarm manager
and one worker, with the same CPU architecture. CI currently builds **linux/amd64**.
Both nodes run the same API and worker image. Swarm's overlay network provides
`db` service discovery; its ingress routing mesh publishes the API port on both
nodes. Spread preferences encourage one API and worker replica per node but do
not guarantee placement. PostgreSQL has one replica pinned to the only node
with `agenthub.db=true`; a DigitalOcean Volume is mounted on that node.

The automation runs **on the native Linux manager**, using its local Unix-socket
Docker context, Bash 4+, Python 3.10+, Docker CLI/Engine, `flock`, `findmnt`,
`mountpoint`, `readlink`, `find`, and SSH. Use a privileged deployment account
that can inspect Docker and read the PostgreSQL directory. Docker access is
effectively administrative access. Arrange permissions on the shared lock file.
`WORKER_SSH` must be a preverified SSH alias/user@host, with noninteractive
authentication, known host keys, Bash as the remote command shell, and the same
Docker/storage inspection permissions. The scripts never accept new host keys.

Online automation intentionally rejects Docker Desktop, Git Bash and remote
Docker contexts: their daemon filesystem is not the shell's filesystem. The
[Milestone 1 runbook](../deploy/swarm/README.md) remains the Desktop procedure.
Offline checks work on Windows; the consolidated rehearsal below uses separate
native Linux VMs. Do not point the new apply command at the running M1 stack.

## Private networking and node setup

Provisioning and the following commands are operator steps for a later approved
deployment. Review DigitalOcean's current network and storage configuration first.

| Port | Allowed source/destination |
| --- | --- |
| TCP 22 | Operator to each node; manager to worker for inspection |
| TCP 2377 | Worker private IP to manager private IP (Swarm control) |
| TCP/UDP 7946 | Only the two node private IPs (discovery) |
| UDP 4789 | Only the two node private IPs (overlay traffic) |
| TCP 18000, or configured API port | Deny public access for this milestone; access using an SSH tunnel |
| TCP 5432 | Never published on either host |

Apply these restrictions with DigitalOcean Cloud Firewalls and appropriate host
rules. Docker-published ports can bypass ordinary UFW rules; verify reachability
externally. Do not expose the Docker API or Swarm ports to the public Internet.
This stack has no HTTPS and uses HTTP Basic authentication. Use a tunnel for the
demo; public access requires a separately reviewed TLS design. Overlay application
traffic is not encrypted by this stack; it is restricted to the VPC.

On the intended manager, after Docker installation (substitute real private IPs):

```bash
docker context show
docker info --format '{{.Swarm.LocalNodeState}}'
docker swarm init --advertise-addr MANAGER_PRIVATE_IP --data-path-addr MANAGER_PRIVATE_IP
```

Privately transfer the worker join command from `docker swarm join-token worker`;
do not paste the join token into reports, history or source control. On the worker,
run it with `--advertise-addr WORKER_PRIVATE_IP --data-path-addr WORKER_PRIVATE_IP`.
Then on the manager:

```bash
docker node ls
docker node update --label-add agenthub.node=manager --label-add agenthub.db=true MANAGER_NODE_ID
docker node update --label-add agenthub.node=worker WORKER_NODE_ID
```

Exactly one node may carry the database label. Its storage must already be mounted.
Do not move the label to an empty directory on another node to recover a database.

## Persistent DigitalOcean Volume

Attach the Volume to the designated database Droplet in the same region. Follow
the [DigitalOcean mount procedure](https://docs.digitalocean.com/products/volumes/how-to/mount-unmount/)
for the actual device and filesystem. **Do not format an existing Volume.** Record
its filesystem UUID and use a persistent mount such as `/mnt/agenthub-volume`.
Use an appropriate UUID-based `/etc/fstab` entry. Arrange a Docker systemd mount
dependency (for example `RequiresMountsFor=/mnt/agenthub-volume`) so database tasks
cannot start against the root disk when the Volume is absent after reboot.
Review and test this host configuration separately; these scripts do not install it.

Only after mounting, create the intended empty `postgres` subdirectory for a fresh
deployment. Reuse the existing directory for subsequent releases. Never clear it
to make a check pass. Useful read-only checks on that node:

```bash
mountpoint /mnt/agenthub-volume
findmnt -n -o SOURCE,UUID,TARGET,OPTIONS --target /mnt/agenthub-volume/postgres
```

Set `POSTGRES_VOLUME_MOUNT`, `POSTGRES_VOLUME_UUID` and `POSTGRES_DATA_PATH` to
these actual values. Preflight requires a canonical directory on the exact mounted,
writable filesystem, with either no contents or `PG_VERSION=17`. This is a mount
check, not a backup, integrity check or test of available disk capacity. It also
rejects other services/containers whose bind mounts overlap this data directory.
Volume mount persistence remains a host responsibility after preflight finishes.

## Image delivery and registry access

The manually triggered **Release runtime image** workflow reuses Backend CI:
browser regressions, Compose configuration, runtime/test image build, backend
lint/tests against the isolated test DB, and live API/worker smoke. Separate
offline deployment tests and Swarm config parsing must also pass. CI exports the
tested runtime image; the publisher loads that exact archive and pushes it to:

```text
ghcr.io/<lowercase-owner>/<lowercase-repository>/backend:sha-<full-commit>-run-<run-id>-<attempt>
```

There is no `latest` tag and no deployment job. Only the publishing job has
`packages: write`; authentication uses the short-lived `GITHUB_TOKEN` via stdin.
The archive contains the runtime image, not the test image or generated `.env`.
The artifact expires after one day. Normal PR/push CI behavior is retained;
artifact export is enabled only by the reusable workflow caller. The workflow
must be present on the default branch for normal manual triggering in GitHub's UI.
Repository/package policy must permit publication. See
[GHCR authentication](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

Use the **digest** printed in the successful release summary for `AGENTHUB_IMAGE`.
Tags are versioned for traceability but a registry administrator can still overwrite
them. Select and record a tested official PostgreSQL **17** image digest separately.
Do not upgrade PostgreSQL with this application deployment script: it refuses a
different database image on an existing stack, even within major version 17.

For private GHCR images, log in on each node using a read-only package credential
through `docker login ghcr.io --password-stdin`; never place it in configuration
or command arguments. The manager must be logged in for manifest checks and
`--with-registry-auth`; pre-pull both exact image references on **both** nodes:

```bash
docker pull "$AGENTHUB_IMAGE"
docker pull "$POSTGRES_IMAGE"
```

Preflight does not pull or build anything. It checks registry manifests, cached
images on each node, architecture compatibility and PostgreSQL major version.
Application image behavior is verified by CI and later smoke checks. Renew
expiring registry credentials before future deployments or rescheduling.

## Configuration and secrets

From a reviewed checkout on the manager:

```bash
cp deploy/swarm/config.example.env .env.cloud
# Edit only non-secret settings and replace every placeholder in .env.cloud.
set -a
source .env.cloud
set +a
```

This is a trusted shell configuration, not untrusted input. `source` executes it.
Do not enable shell tracing or print rendered stack configuration. The example
contains secret **names**, never secret values. Store the real independent database
and developer passwords privately. Create the corresponding Swarm secrets once,
using a hidden prompt and stdin on the manager (repeat with a different password
and `$BOOTSTRAP_PASSWORD_SECRET`):

```bash
read -r -s -p 'Database password: ' secret_value; printf '\n'
test -n "$secret_value" && printf '%s' "$secret_value" | docker secret create "$DB_PASSWORD_SECRET" -
unset secret_value
```

Use strong independently generated passwords from a password manager. Do not
save secrets in shell history, CI artifacts or deployment reports. Do not delete
or overwrite existing secrets. Existing PostgreSQL initialization does not reset
database passwords, and bootstrap preserves existing account passwords. Secret
rotation needs a separate coordinated procedure; changing names alone is unsafe.

## Deployment procedure

These scripts reuse `deploy/swarm/stack.yaml`; they do not generate another stack
or rely on `depends_on`. They never create Swarm, labels, storage, secrets or cloud
resources. Plan mode parses the stack locally without contacting the daemon,
registry or SSH hosts. Online preflight is read-only.

```bash
bash deploy/scripts/preflight.sh --plan
bash deploy/scripts/deploy.sh --plan
bash deploy/scripts/preflight.sh --check
# Run only during the approved deployment/rehearsal window:
bash deploy/scripts/deploy.sh --apply
```

Apply acquires a nonblocking manager-local `flock`, shared across operators and
checkouts, then repeats preflight under the lock. All operators must use the same
`DEPLOY_LOCK_PATH` and this script. Manual Swarm commands bypass the lock; do not
mix manual migration/deploy operations with automation. This two-node design
supports exactly one manager. It is not a distributed migration lock.

The sequence is:

1. Deploy the unchanged stack with API, worker and setup replicas zero. On an
   existing deployment this intentionally causes downtime. Wait for previous
   application/setup tasks to terminate, and for the sole DB task to be healthy.
2. Snapshot historical setup task IDs. Force one setup task with restart disabled.
   It executes `alembic upgrade head` followed by `python -m app.manage bootstrap`.
   Only a new task reaching `complete` with exit code zero counts as success;
   stale successes, multiple new tasks, failure and timeout do not.
3. Return setup to zero. Scale API/worker to the requested counts and verify every
   desired task is running and container-healthy on its actual node. Paused updates
   fail. Default timeout is 300 seconds per wait, adjustable for slower hosts.

On migration failure/timeout the script stops, leaves applications at zero and
leaves setup armed so a later invocation refuses to rerun it blindly. It neither
deletes data nor rolls back a schema. Inspect task history/logs privately, determine
whether setup is still executing or partially succeeded, and obtain a reviewed
recovery decision. Only after resolving the cause and confirming no setup task is
active should an operator return setup to zero. A subsequent apply deliberately
runs setup again; Alembic's applied revisions and bootstrap's existing-user behavior
help, but do not make arbitrary failed migrations safe to retry. Killing the
deployment shell does not cancel an already scheduled setup task.

Application convergence failure can leave partial replicas running; the script
reports failure without destructive recovery. Queue/lease state remains in the
database. This procedure does not prove recovery of an interrupted in-flight job.

## Verification and saved history

Read service/task state on the manager; inspect logs privately if needed:

```bash
docker stack services "$STACK"
docker service ps --no-trunc "${STACK}_db"
docker service ps --no-trunc "${STACK}_setup"
docker service ps --no-trunc "${STACK}_api"
docker service ps --no-trunc "${STACK}_worker"
```

From the operator workstation, open an SSH tunnel using a free local port:

```bash
ssh -N -L 18000:127.0.0.1:18000 agenthub-manager
```

`smoke.py` uses only the standard library and the existing HTTP API. Set
`SMOKE_USERNAME` and either `SMOKE_PASSWORD_FILE` (a private file outside the repo)
or `SMOKE_PASSWORD` in the process environment; do not pass a password on the CLI.
Use the existing account password, which may differ from a newer bootstrap secret.
It rejects plain HTTP except on loopback, refuses redirects, and disables ambient
HTTP proxies. Never publish the API without transport protection.

```bash
python3 deploy/scripts/smoke.py --plan
# Create the parent directory privately, outside the repository, first.
python3 deploy/scripts/smoke.py --write --record "$HOME/agenthub-validation/release-1.json"
python3 deploy/scripts/smoke.py --verify-state "$HOME/agenthub-validation/release-1.json"
```

Write mode creates exactly one synthetic conversation and job, repeats the same
submission key, waits for completion, and compares two complete history reads.
It requires fixed mode to avoid model calls. The receipt stores IDs, endpoint,
account name, message count and a canonical SHA-256 of every returned message
field, including content and timestamps; it contains no password or message text.
It is created exclusively with mode 0600 on Linux. Protect it with Windows ACLs
when using Windows. An existing receipt is never overwritten. Failed runs leave
an incomplete checkpoint when possible; do not automatically repeat a conversation
creation after an ambiguous network failure.

`--verify-state` sends GET requests only and compares the same conversation/job
with the saved baseline. Run it after a separately approved persistence test to
prove history survived that event. Merely running write mode does **not** establish
container-replacement persistence. This tool does not restart services or delete
its synthetic records. Conversation metadata lookup uses the API's recent-500
list limit; the rehearsal account should be quiet during the check.

## Safe local checks and consolidated integration plan

From Windows PowerShell, without contacting the running stack:

```powershell
Get-ChildItem deploy/scripts/*.sh | ForEach-Object {
    & 'C:\Program Files\Git\bin\bash.exe' -n $_.FullName
    if ($LASTEXITCODE -ne 0) { throw 'Bash syntax check failed' }
}
python -m unittest discover -s deploy/tests -v
python deploy/scripts/smoke.py --plan
python deploy/scripts/monitor.py --plan
```

Use the existing runbook's placeholder environment for `docker stack config`.
Unit tests use temporary files, a fake Docker CLI and a mock API on an ephemeral
loopback port; they never use the local validation stack or its credentials.

## Read-only monitoring

Run `monitor.py` on the Swarm manager with Docker CLI access. It performs one
point-in-time check: GET `/health` to verify API availability and the existing
API-to-PostgreSQL `SELECT 1` probe; read the `api`, `worker`, `db`, and `setup`
replica counts; and verify the configured number of Swarm nodes are Ready and
Active. Set `--expected-nodes 1` for a single-node local validation or
`--expected-nodes 2` for the two-Droplet deployment. It expects the configured
API/worker counts, one DB replica, and setup at zero.

For every running API, worker, and PostgreSQL replica, it separately checks the
Swarm task state, the container process state, and the container health status.
It inspects containers on the manager locally and uses the preverified
`WORKER_SSH` target with strict host-key checking for tasks on the other node.
All three services must have a Docker health check configured and report
`healthy`. The command issues only health/API reads and Docker inspection/list
commands; it does not update services, write database records, or create chat
jobs. For a remote operator, use an SSH tunnel and a loopback URL; no Basic
credentials are needed because the monitor uses the public health endpoint.

```bash
python3 deploy/scripts/monitor.py --plan
python3 deploy/scripts/monitor.py --stack agenthub --expected-nodes 2 --url http://127.0.0.1:18000
# On a single-node local validation stack, use --expected-nodes 1.
```

This is a manual snapshot, not a continuous monitor, worker-progress check,
alerting system, or proof of failure recovery. DigitalOcean Metrics and Alerts
remain a later operator task: select both Droplets and the attached Volume,
review available CPU, memory, disk, and network metrics, then create threshold
alerts for sustained CPU/memory pressure, low disk space, and node/volume
availability. Route notifications to an agreed team channel and validate each
alert with a safe test before relying on it. Do not use a test that stops services
or fills the database volume. Keep application health and queue-progress checks
separate from infrastructure metrics; this milestone does not configure cloud
resources, notification destinations, thresholds, or alert tests.

After review, run **one isolated rehearsal** on two native Linux VMs/Droplets,
using a new stack name, dedicated secrets, unused API port and a fresh mounted
test filesystem. Provisioning still requires a separate decision.

1. Run offline checks, then exercise preflight failures: missing variable/secret,
   missing or duplicate DB label, missing/wrong/read-only mount, wrong worker SSH
   target, missing cached image, wrong architecture, unavailable registry and
   port/data-path conflict. Confirm no deployment mutation occurs on failure.
2. Manually trigger release on the reviewed revision. Verify every gate, GHCR
   permission, digest summary, and pulling the exact image on both nodes. Configure
   those digests and inspect the plan and read-only preflight.
3. Apply to the isolated stack. Record one healthy DB, exactly one new successful
   setup task, setup returned to zero and API/worker at 2/2. Confirm each node's
   health/placement and that port 5432 is not externally reachable.
4. Run smoke write/verify through the SSH tunnel. Save the receipt outside Git.
   Repeat the approved DB-task replacement, idle-worker failure and API-distribution
   checks from M1, using this rehearsal stack only. Compare the same saved history
   after recovery. Do not claim interrupted-job recovery without a separate test.
5. Reapply a compatible application release while retaining database image, mount
   and secret. Observe planned downtime and migration serialization. In this
   disposable rehearsal, test a second concurrent invocation, failed setup and
   timeout; prove there is no application start or blind migration retry on failure.
   Confirm a historical successful task cannot mask a new failed one.
6. With separate approval, reboot the DB host to validate the actual mount dependency
   and persistence; script preflight alone cannot establish reboot safety. Record
   timings, commands and limitations. Keep cleanup explicitly separate from tests.

## Known limitations and course review

One manager is not a control-plane quorum; one pinned DB and its Volume are not
database HA. Swarm cannot reschedule that DB safely to an unlabeled/empty node.
Volume attachment is not a backup. This milestone adds no TLS, DigitalOcean
metrics/alerts, backups, automatic rollback, migration repair, infrastructure
provisioning or automatic production deployment. The read-only monitor and worker
health checks establish API/database availability, while the smoke job checks
processing. There is no multi-architecture image build.
The automation intentionally does not change existing DB images, storage paths,
DB/user names or database secrets; those changes need a separate procedure.

Confirm lecture/rubric expectations for manager availability, external versus
Swarm load balancing, recovery evidence, monitoring, backups and what qualifies
as CD before claiming the full course requirements are met. The proposal still
uses Docker Swarm; this milestone introduces no Kubernetes. Docker documents the
[legacy Compose v3 stack deployment path](https://docs.docker.com/engine/swarm/stack-deploy/)
used by the existing stack.
