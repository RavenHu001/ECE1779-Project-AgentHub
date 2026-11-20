# AgentHub — Cloud Deployment & Operations Test Report

**Project:** ECE1779 AgentHub  
**Test date:** November 20, 2026  
**Environment:** DigitalOcean Toronto (TOR1) and an isolated local Docker test environment  
**Component:** Cloud & Delivery  
**Overall result:** **PASS for all tests performed**, with the untested end-to-end delivery items listed separately below.

## 1. Test environment

The deployed AgentHub stack uses two DigitalOcean Droplets in a Docker Swarm cluster (one manager and one worker), a persistent DigitalOcean Block Storage Volume for PostgreSQL 17, private DigitalOcean Spaces for backups, and Caddy as the HTTPS reverse proxy. The application is configured with two API replicas and two background-worker replicas.

| Component | Test configuration |
| --- | --- |
| Orchestration | Two-node Docker Swarm (manager + worker) |
| API | 2 replicas |
| Background workers | 2 replicas |
| Database | PostgreSQL 17, 1 replica |
| Persistent storage | DigitalOcean Block Storage Volume mounted on the database host |
| Backup storage | Private DigitalOcean Spaces bucket, TOR1 |
| Scheduled backup | systemd timer at 03:00 UTC |
| Public HTTPS test endpoint | `https://201-79-149-128.sslip.io/health` |
| Local restore/rolling-update test host | Docker Desktop, with a separate Swarm-inactive Docker daemon for restore |

## 2. Completed tests — PASS

### 2.1 Two-node Docker Swarm and application health — PASS

**Procedure:** Checked Swarm node readiness, deployed service replica counts, and the API's `/health` response on the manager.

**Observed results:**
- Both Swarm nodes were reported ready and active.
- `agenthub_api`: **2/2**; `agenthub_worker`: **2/2**; `agenthub_db`: **1/1**.
- The database setup service was scaled to **0/0** after initialization.
- The health endpoint returned:

```json
{"status":"ok","database":"ok","reply_mode":"fixed"}
```

**Result:** **PASS** — the cluster and application/database health checks succeeded.

### 2.2 PostgreSQL persistent volume — PASS

**Procedure:** Verified that the PostgreSQL data directory used the attached DigitalOcean Volume rather than an ephemeral container filesystem, and checked the volume mount and capacity.

**Observed results:**
- The volume was mounted at `/mnt/agenthub_postgres`.
- The filesystem was approximately **9.8 GB**, with around **48 MB used** at the time of inspection.
- Persistent database storage and service operation were verified in the deployed stack.

**Result:** **PASS** — the intended persistent storage was present and in use.

### 2.3 Real PostgreSQL backup to DigitalOcean Spaces — PASS

**Procedure:** Ran a real PostgreSQL 17 logical backup, uploaded it to a private DigitalOcean Spaces bucket, and checked the uploaded archive against its SHA-256 digest.

**Observed results:**
- The real backup command completed successfully (`ExecMainStatus=0`).
- The backup archive was uploaded to the private Spaces bucket.
- Verification of the uploaded archive's SHA-256 checksum succeeded.
- The configured retention policy is **seven successful UTC daily backups**; the retention algorithm passed offline tests. Only **one daily backup** had accumulated during the observed live run, so seven-day live pruning was not exercised.

**Result:** **PASS** — live backup creation, upload, and checksum verification succeeded.

### 2.4 Scheduled backup execution — PASS

**Procedure:** Enabled the `agenthub-backup.timer` systemd timer and inspected its real execution log.

**Observed results:**
- The scheduled job ran on **November 20, 2026, at 03:00 UTC**.
- The service completed successfully.
- The job revalidated the existing daily archive and reported **1 retained backup, 0 pruned**.
- The timer scheduled its next run for the following day.

**Result:** **PASS** — the scheduled backup ran successfully without manual invocation.

### 2.5 Restore drill from a real Spaces backup — PASS

**Procedure:** Downloaded the actual Spaces `.dump` archive and ran `restore_test.py` inside an independent, **Swarm-inactive Docker-in-Docker daemon** on the local Docker Desktop host. This avoided touching the DigitalOcean production database or the existing local validation Swarm.

**Observed results:**
- The original checksum-bearing archive filename was preserved.
- The restore tool accepted the archive and the pinned PostgreSQL 17 image digest.
- A fresh network-isolated PostgreSQL container was created.
- The database restore completed and AgentHub schema checks passed.
- The temporary restore container and its storage were removed.

**Success output:**

```text
Restore passed. Verified AgentHub schema and removed the test container and its storage.
```

**Result:** **PASS** — a real off-host logical backup was successfully restored and schema-validated. This test did not validate every application record or perform a full disaster-recovery cutover.

### 2.6 DigitalOcean monitoring — PASS (configuration and agent checks)

**Procedure:** Checked the DigitalOcean monitoring agents and configured resource alerts for both Droplets.

**Observed results:**
- DigitalOcean monitoring agents were active and enabled on both the manager and worker.
- Alerts were configured for **CPU ≥ 80%**, **memory ≥ 85%**, and **root disk ≥ 80%**.

**Result:** **PASS** — agents and alert configuration were established. Actual alert email delivery and threshold-trigger behavior were not tested.

### 2.7 Local Swarm rolling update and rollback — PASS

**Procedure:** On the Docker Desktop validation Swarm, performed a real API service rolling update by changing a temporary container label, then rolled back to the prior service specification. Checked replica counts, configuration, and API health.

**Observed results:**
- The API converged at **2/2** after rolling update.
- The API converged at **2/2** after rollback.
- The original image and container labels were restored.
- `/health` returned `status=ok` and `database=ok`.

**Success output:**

```text
PASS: Rolling Update and Rollback restored original configuration
```

**Result:** **PASS** — local service-level rolling replacement and rollback worked. The test did not replace the application image with a new version.

### 2.8 Real two-node DigitalOcean rolling update and rollback — PASS

**Procedure:** On the production two-node Swarm, changed only a temporary API container label, used a rolling update configured for one replica at a time, then executed one rollback. The test did **not** run a database migration or change the application image.

**Observed results:**
- The cloud API service converged after the rolling update and rollback.
- Final service counts were **API 2/2**, **Worker 2/2**, **DB 1/1**.
- `/health` returned `status=ok`, `database=ok`, and `reply_mode=fixed`.
- The exact original API image and container labels were restored.

**Success output:**

```text
PASS: Cloud rollback restored original image and labels
```

**Result:** **PASS** — real cloud service-level rolling update and rollback succeeded. A release with a different application image digest was not tested.

### 2.9 Public HTTPS / TLS with Caddy — PASS

**Procedure:** Ran Caddy on the manager with a temporary `sslip.io` hostname, checked public DNS, verified automatic HTTP-to-HTTPS redirection and certificate validation, and accessed the API through the public HTTPS endpoint.

**Observed results:**
- `201-79-149-128.sslip.io` resolved to the manager's public IP.
- HTTP returned **308 Permanent Redirect** to the corresponding HTTPS URL.
- HTTPS returned **200 OK** with a valid certificate accepted by `curl.exe` without `-k`.
- The response included `Via: 1.1 Caddy` and `Strict-Transport-Security: max-age=31536000`.
- The API returned:

```json
{"status":"ok","database":"ok","reply_mode":"fixed"}
```

- Caddy logs showed successful Let's Encrypt ACME domain authorization.

**Result:** **PASS** — public HTTPS, trusted TLS, redirect, reverse proxy, and application/database health were verified end to end.

### 2.10 Cloud automation and configuration checks — PASS

**Procedure:** Ran focused offline tests for backup lock retries, restore isolation, proxy behavior, migration failure handling, rolling updates, and application-only rollback. Validated the backup systemd unit on Ubuntu and parsed the Caddy configuration.

**Observed results:**
- Codex's focused test report: **23 passed, 0 failed, 1 skipped** (the skip was for unavailable Linux `systemd-analyze` in the Windows test environment).
- The updated systemd backup unit passed `systemd-analyze verify` on the Ubuntu manager.
- The Caddy configuration passed adaptation checks.
- GitHub Actions workflow lint passed in the focused offline validation.

**Result:** **PASS** — the checks executed successfully. The new scheduled Python retry implementation was not separately demonstrated through four real lock-contention attempts on the production timer.

## 3. Not performed / not fully verified

The following items must **not** be presented as passed end-to-end tests:

| Item | Status / limitation |
| --- | --- |
| GitHub Environment human approval | **Not live-verified.** The `production` environment and independent-review checks exist in workflow code, but the current account lacked repository Settings access to configure/test protected approvals. |
| Full CI/CD deployment of a new image | **Not live-verified.** No GitHub-approved deployment of a *new image digest* followed by application-image rollback was executed. |
| Live certificate renewal | **Not yet observed.** Initial HTTPS certificate issuance and validation succeeded; a renewal cycle was not tested. |
| Authenticated SSE through HTTPS | **Not end-to-end verified.** Caddy/SSE behavior has offline coverage, but a live authenticated stream was not exercised. |
| Seven-day backup pruning | **Not yet observed live.** Retention tests passed offline, but only one daily backup was present during the live check. |
| New backup lock-retry unit in production | **Configuration verified on Ubuntu; rollout/runtime contention not verified.** The corrected unit had been validated as a staged file; the updated backup script and unit still need coordinated installation when scheduled. |
| Automatic Caddy restart after host reboot | **Not verified.** The temporary HTTPS test container was started with `--restart no`. |

## 4. Conclusion

**All executed Cloud & Delivery functional tests passed.** The project demonstrated a working DigitalOcean two-node Swarm deployment, persistent PostgreSQL storage, real off-site backups and isolated restoration, configured infrastructure monitoring, real local and cloud service-level rolling updates/rollbacks, and publicly accessible HTTPS using a trusted TLS certificate.

The remaining acceptance work concerns live GitHub Environment approval, a new-image CI/CD rollout and rollback, and longer-running operational behavior. These are clearly separated from the completed tests above.
