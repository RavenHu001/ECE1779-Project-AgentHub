# Local Swarm validation report

**Result: passed.** Authenticated history verification, PostgreSQL persistence,
worker replica recovery, and API replica distribution were verified locally.
The user explicitly approved the PostgreSQL replacement and one-worker interruption.

- Date: November 13, 2026 (America/Toronto); timestamps below are UTC.
- Source revision: `7008d9f43eed562352e8309dca8d21feb7ec42ca`.
- Docker context: `desktop-linux`; one Linux manager, `docker-desktop`.
- Target stack: `agenthub-m1-local-validation`; reply mode: `fixed`.
- Application image: `agenthub-swarm-local:7008d9f-validation`.

## Authenticated baseline ? passed

Credentials were read privately from the ignored `.env.swarm-validation` file,
used in an in-memory HTTP Authorization header, and never printed, passed in
command-line arguments, or copied into this report. No account password was reset.

At `2026-11-13T06:56:01.181941+00:00`, authenticated GET requests to `/me`,
`/conversations?limit=500`, and the paginated message endpoint succeeded:

```text
GET /conversations/874ec1bf-244b-48e0-903d-02f5fd3ef0e9/messages?limit=500&after_seq=0
```

The client followed `after_seq` until an empty page. The existing `developer`
account retrieved the same conversation and all **six messages**. This matched
the earlier read-only SQL baseline. No replacement conversation or test job was created.

- Conversation ID: `874ec1bf-244b-48e0-903d-02f5fd3ef0e9`.
- Complete normalized history SHA-256 before and after both tests:
  `94f8cc1c3f5c76b82b9339312244e57f98cc8c71f27e456be9cb5d15d693c895`.
- Comparison covered conversation metadata and every message ID, conversation ID,
  role, content, sequence, reply reference and timestamp. Timestamps were normalized
  to UTC ISO format; JSON keys were sorted with compact separators and UTF-8 encoding.
- Complete history was compared, not only the count or hash. Message contents are
  retained only in the private local temporary baseline, not in this report.

## PostgreSQL persistence ? passed

All services were healthy and no jobs were queued or running immediately before
replacement. The actual command was issued exactly once:

```powershell
docker --context desktop-linux service update --detach=true --force agenthub-m1-local-validation_db
```

Command output:

```text
agenthub-m1-local-validation_db
```

| Evidence | Before | After |
| --- | --- | --- |
| Task ID | `5ok2tovg2xdhzgzw83kr4kgs9` | `q3r131kbptabf2o6d6n1vj5c0` |
| Container ID | `a73ac1aa80d3` | `b5eaadf5324b` |
| Database health | healthy | healthy |
| Authenticated message count | 6 | 6 |
| Bind source, image, history | Baseline recorded | Exact match |

The old database task exited with code **0**.
The replacement retained these verified storage settings:

```text
Type: bind
Source: /mnt/docker-desktop-disk/data/agenthub-swarm-m1-local-validation/postgres
Destination and SQL data_directory: /var/lib/postgresql/data
Database: agenthub_swarm_validation
Image: postgres@sha256:40865bc877a289da75963d97d72b59633f639fc5af7a4e856bfa3714cae5b536
```

The service specification was unchanged except for the expected `ForceUpdate`
increment. PostgreSQL remained at one replica with no published database port.
No directory, volume, secret, schema, or image was replaced by the test commands.

- Replacement requested: `2026-11-13T06:56:45.912488+00:00`.
- New database plus API/worker health observed after **9.705 seconds**.
- Full authenticated history comparison passed at `2026-11-13T06:56:56.017950+00:00`.
- Both original API containers remained running; worker containers also remained
  unchanged during the database test.
- Read-only authenticated SQL confirmed migration `0003_read_only_refund_check`,
  the same database/data directory, and zero active jobs.
- Swarm's update-monitoring period completed at
  `2026-11-13T06:58:54.959257864Z` with `State=completed` and `Message=update completed`.

Recovery timing measures when polling observed healthy services, not exact
HTTP downtime. Transient unavailability between observations was not measured.

## One worker replica failure and recovery ? passed

This test ran only after the persistence test passed. Both workers were healthy
and the read-only job count was zero immediately before interruption. One current
container was checked against the target service labels and killed exactly once:

```powershell
docker --context desktop-linux kill --signal SIGKILL 430fa8001b7387870bd3e37f039bf8822d094866a7099123e23ce59b2dee00f8
```

Docker returned that container ID. The desired replica count stayed at two;
no service update or scale command was issued for the worker.

| Evidence | Result |
| --- | --- |
| Interrupted task | `mgnduciohr54gs0eal9zhr0oi` |
| Interrupted container | `430fa8001b73` |
| Old task result | `failed`, exit code `137` |
| Replacement task | `ru0t19mhx24svzy1geb5disce` |
| Replacement container | `1f856982daa2` |
| Untouched surviving container | `aed712bd1b11` |
| Healthy 2/2 recovery observed | **11.640 seconds** after interruption |
| Queued/running jobs before and after | 0 / 0 |

Interruption started at `2026-11-13T06:57:41.579343+00:00`; verification finished at
`2026-11-13T06:57:54.089291+00:00`. Task history was inspected with:

```powershell
docker --context desktop-linux service ps --no-trunc agenthub-m1-local-validation_worker --format '{{.ID}} {{.Name}} {{.CurrentState}} {{.Error}}'
```

It showed the old task failing with `task: non-zero exit (137)`, its replacement
running, and the second original task still running. Replacement startup log:

```text
2026-11-13 06:57:48,085 INFO worker=1f856982daa2-046bb4f9 started mode=fixed
```

The surviving worker retained its container ID and original start time. The
worker service specification did not change, and authenticated history remained
identical. **This verifies replica recovery only. No interrupted-job recovery,
lease reclamation, or exactly-once processing claim follows from this idle test.**

## API distribution ? passed without interruption

During the earlier read-only preflight, 20 sequential `/health` requests used
separate HTTP connections and `Connection: close`. The correlation marker was
`agenthub-preflight-f95810ed40114225862f1fddc6bf8a57`, with `n=0` through `n=19`:

```text
GET /health?validation=agenthub-preflight-f95810ed40114225862f1fddc6bf8a57&n=0
```

Each returned HTTP 200. Per-container `docker logs --since <request-start-UTC>`
output was filtered to this marker, avoiding confusion with health probes:

| API task | Container | Correlated requests |
| --- | --- | --- |
| `r7f4t1hkd2u6qp1bes9cict41` | `17905fe1720f` | 10 |
| `g7cc0qpcnuftl9qqj2itd73k4` | `23ead6ceedc7` | 10 |

All 20 requests were accounted for across both replicas through the published
port 18000. Application code was not modified to obtain this evidence.

## Final state and preservation checks

Actual final service-status command:

```powershell
docker --context desktop-linux service ls --filter label=com.docker.stack.namespace=agenthub-m1-local-validation --format '{{.Name}} {{.Replicas}}'
```

Observed output:

```text
agenthub-m1-local-validation_api 2/2
agenthub-m1-local-validation_db 1/1
agenthub-m1-local-validation_setup 0/0
agenthub-m1-local-validation_worker 2/2
```

All five running containers are healthy. Final `/health` output:

```json
{"status":"ok","database":"ok","reply_mode":"fixed"}
```

The final history/health check passed at `2026-11-13T06:58:58.331316+00:00`. Before/after inventory
checks found unchanged state/mount/image metadata for **7 unrelated containers**,
unchanged records for **3 existing volumes** and **2 secrets**, and unchanged hashes
for **64 repository files other than this report**. No unrelated data was mounted
or written by the validation commands. Existing user-created files were preserved.

Only this report was updated in the repository. No application/frontend code,
credentials, Docker volumes or secrets were changed. No cleanup/prune operation,
DigitalOcean action, commit, or push was performed. No rollback was needed.

## Limits

- Single-node Docker Desktop testing does not demonstrate multi-node failure
  tolerance, manager/database high availability, or DigitalOcean Volume mounting.
- Persistence was verified across one graceful database task replacement, not
  host loss, disk loss, backup restoration, or an abrupt PostgreSQL crash.
- Worker interruption occurred while idle, with no new jobs submitted.
- Load balancing was a 20-connection observation, not a throughput benchmark or
  guarantee of equal distribution for persistent connections.
- These are local Swarm runtime checks in fixed mode, not live model-provider
  checks or a rerun of the complete Compose regression suite.
