# Backend Manual Testing Guide

Use this guide to manually check the current Docker backend with Swagger UI or Postman. The implemented scope is authenticated chat, durable jobs and fixed English replies. These cases do not test model integration, approvals, SSE or refunds.

For setup details, see the [team guide](../README.md). The [Chinese companion](developer/zh-CN/MANUAL_TESTING.md) uses the same case IDs and result fields.

## Prepare the test environment

Run commands from the repository root. For a new independent checkout, first run `python scripts/init_env.py` (`python3` on Linux if necessary), then start the environment:

```bash
docker compose up -d --build --wait
docker compose ps -a
```

For a shared environment that is already running, use its assigned endpoint and ask the maintainer for service status instead of running startup or update commands yourself.

Use your assigned account, referred to below as **account A**. For your own initial environment, read `BOOTSTRAP_USERNAME` and `BOOTSTRAP_PASSWORD` privately from `.env`. Never include passwords, authorization headers or `.env` contents in test evidence.

Open <http://127.0.0.1:8000/docs>. For a remote server, first open an SSH tunnel:

```bash
ssh -N -L 18000:127.0.0.1:8000 your-user@your-server
```

Then use <http://127.0.0.1:18000/docs>. Use the same host and port as the base URL in Postman. In Swagger, select **Authorize**, enter account A's credentials, and use **Try it out → Execute** for each operation. In Postman, select **Basic Auth**.

MT01–MT13 use API calls and create test records. MT13 also needs a second account. **MT14 and MT15 stop or recreate services: run them in your own isolated environment with one worker. Coordinate with the maintainer before any equivalent action on a shared server.** Mark unavailable cases `BLOCKED` rather than stopping shared services unexpectedly.

Generate a new UUID for every genuinely new submission:

```powershell
[guid]::NewGuid()
```

Alternatively:

```bash
python -c "import uuid; print(uuid.uuid4())"
```

Keep the identifiers returned during testing:

| Name | What to record |
| --- | --- |
| `C1` | The conversation created by account A in MT03; continue using this one |
| `K1` | The submission key used in MT05 |
| `M1` | The user message ID returned in MT05 |
| `J1` | The job ID returned in MT05 |
| `J2` | The job ID created while the worker is stopped in MT14 |

Use the actual UUIDs in URL parameters; `C1`, `M1` and `J1` are labels, not valid UUID values.

## Normal operation

### MT01 Service startup and health

Inspect `docker compose ps -a`, then call `GET /health` without authentication.

Expected: database, API and worker are running, the API is healthy, and `migrate` and `bootstrap` have exited with code 0. HTTP `200` returns:

```json
{"status": "ok", "database": "ok", "reply_mode": "fixed"}
```

### MT02 Account authentication

Authenticate as account A and call `GET /me`. Expected: HTTP `200` with that account's ID and username, without a password hash. Repeat with an incorrect password; expect `401`, then restore the correct credentials.

### MT03 Create a conversation

Call `POST /conversations`:

```json
{"title": "Manual backend test"}
```

Expected: HTTP `201` with the title and conversation ID. Record this ID as `C1`.

### MT04 English default title

Call `POST /conversations` again with `{}`. Expected: HTTP `201` and title `New conversation`. This creates an additional conversation; keep using the original `C1` for the remaining cases.

### MT05 Submit a message

Call `POST /conversations/{C1}/messages` with a new UUID as the submission key:

```json
{
  "content": "Hello",
  "submission_key": "cbd2fe4d-3781-4856-b416-44242a730dac"
}
```

Expected: HTTP `202` with `message_id` and `job_id`. Record the key as `K1`, and the returned IDs as `M1` and `J1`. Acceptance does not mean processing has finished.

### MT06 Read the reply and job state

Call `GET /conversations/{C1}/messages` and `GET /jobs/{J1}`. Refresh for up to 30 seconds under the default configuration.

Expected: one user message `Hello`, followed by one assistant reply `Message received.`. Their sequence numbers are 1 and 2, and the reply's `reply_to_message_id` equals `M1`. `J1` finishes as `SUCCEEDED`. A fast worker may complete before you observe `QUEUED` or `RUNNING`.

## Duplicate requests and invalid input

### MT07 Repeat the same submission

Repeat MT05 with the exact same `C1`, `K1` and `Hello` content. Expected: HTTP `202` with the original `M1` and `J1`. The conversation still contains only the original request and its reply.

### MT08 Reuse a key with different content

Repeat the same request using `K1` but change content to `Different text`. Expected: HTTP `409`; the existing conversation history is unchanged.

### MT09 Reject blank content

Use a fresh UUID and submit `content` as `""`, then repeat with `"   "`. Expected: HTTP `422` each time and no new messages. These are validation failures, not worker failures.

### MT10 Reject an unknown conversation

Generate a valid UUID that has not been used to create a conversation. Call `GET /conversations/{that UUID}/messages`. Expected: HTTP `404`. A malformed UUID tests validation and returns `422`, which is a different case.

### MT11 Preserve Unicode input

Submit a new message to `C1` with a fresh key. The following JSON uses Unicode escapes for a non-English greeting:

```json
{
  "content": "\u4f60\u597d",
  "submission_key": "5c3f0596-6c53-48be-bfd0-b9ca836fcf29"
}
```

Expected: the saved user message retains the decoded Unicode characters. The assistant still replies `Message received.`. The history now contains additional records, so do not expect its total length to remain two.

## Authentication and ownership

### MT12 Reject a request without credentials

In Postman, create a fresh `GET /me` request, choose **No Auth**, and ensure no `Authorization` header is present. Expected: HTTP `401`.

Browsers may reuse cached HTTP Basic credentials, so Swagger's logout alone is not reliable evidence that a request had no credentials. Restore account A authentication afterward.

### MT13 Isolate two accounts

Obtain a separate **account B**. A maintainer can create one with:

```bash
docker compose run --rm admin create-user manual-b
```

Enter a new password interactively; if the account already exists, use an assigned account rather than resetting another tester's password.

Authenticate as B and call `GET /conversations/{C1}/messages`, `POST /conversations/{C1}/messages` with valid message input, and `GET /jobs/{J1}`. Expected: `404` for each. B's `GET /conversations` list must not include `C1`, even if B already owns other conversations.

Switch back to account A and confirm that `C1` is still accessible before continuing.

## Service interruption and persistence

### MT14 Queue work while the worker is stopped

In your isolated one-worker environment:

```bash
docker compose stop worker
```

Submit a new message to `C1`, using a fresh key, and record its job ID as `J2`. Expected: HTTP `202`; the user message is readable, `J2` remains `QUEUED`, and there is no reply for this new message yet.

Restart the worker:

```bash
docker compose up -d worker
```

Expected within 30 seconds under the defaults: `J2` becomes `SUCCEEDED`, and exactly one reply references its source message. Keep the worker running after this case. This tests persistence of unclaimed work, not recovery from a crash halfway through processing.

### MT15 Preserve records after container recreation

Record the message IDs and contents currently returned for `C1`, plus the completed state of `J1`. Keep the same checkout, `.env` and Compose project name, then run:

```bash
docker compose down
docker compose up -d --wait
```

Authenticate as A again and query `C1` and `J1`. Expected: the recorded messages and contents remain, and `J1` is still `SUCCEEDED`. Initialization must not reset the account password.

**Do not add `-v` or `--volumes`: those options delete the database volume.** Container persistence is not a backup-restore test.

## Investigate a failure

Record the case ID, HTTP status, sanitized response, relevant resource IDs and time. Inspect a failed job's `last_error` and the relevant logs:

```bash
docker compose logs --tail 100 api worker
docker compose logs --tail 100 migrate bootstrap db
```

If a job stays queued, check whether a worker is running. If a request returns `503`, check database availability. Record actual behavior before retrying; do not treat a later pass as evidence that the original failure did not occur.

## Test result record

Copy this section for each manual test run. Result values are `NOT_RUN`, `PASS`, `FAIL` or `BLOCKED`. Fill results only after executing a case; previous automated test results do not populate this manual record.

| Run detail | Value |
| --- | --- |
| Tester | To fill |
| Date and time with timezone | To fill |
| Commit tested from `git rev-parse --short HEAD` | To fill |
| Base URL and environment name | To fill |
| Compose project name and worker count | To fill |
| Test tool and version | To fill |
| Account A and B usernames only | To fill |
| Relevant configuration overrides | To fill without secrets |

| Case | Check | Expected result | Actual result and evidence reference | Result |
| --- | --- | --- | --- | --- |
| MT01 | Startup and health | Healthy services; HTTP 200 | To fill | NOT_RUN |
| MT02 | Valid and invalid login | HTTP 200 and 401 | To fill | NOT_RUN |
| MT03 | Create conversation | HTTP 201; save C1 | To fill | NOT_RUN |
| MT04 | Default title | New conversation | To fill | NOT_RUN |
| MT05 | Submit message | HTTP 202; save M1 and J1 | To fill | NOT_RUN |
| MT06 | Read reply and job | Single linked English reply; SUCCEEDED | To fill | NOT_RUN |
| MT07 | Duplicate submission | Same IDs; no extra records | To fill | NOT_RUN |
| MT08 | Conflicting submission | HTTP 409; history unchanged | To fill | NOT_RUN |
| MT09 | Blank content | HTTP 422; no new messages | To fill | NOT_RUN |
| MT10 | Unknown conversation | HTTP 404 for a valid unknown UUID | To fill | NOT_RUN |
| MT11 | Unicode input | Original input retained; English reply | To fill | NOT_RUN |
| MT12 | No credentials | HTTP 401 | To fill | NOT_RUN |
| MT13 | Other account's resources | HTTP 404; C1 absent from B's list | To fill | NOT_RUN |
| MT14 | Stopped worker | QUEUED, then one reply and SUCCEEDED | To fill | NOT_RUN |
| MT15 | Container recreation | Same saved records and account access | To fill | NOT_RUN |

| Follow-up detail | Value |
| --- | --- |
| Totals | PASS: __ / FAIL: __ / BLOCKED: __ / NOT_RUN: __; total 15 |
| Issue links and reproduction steps | To fill |
| Retest date and result | To fill |
| Environment restored | To fill: confirm the worker is running and check pending work and service health |

Use sanitized screenshots, response excerpts or repository issue links as evidence. Preserve the first failure and append retest results rather than overwriting it. On shared environments, leave test data unless the maintainer has agreed on cleanup; do not reset the database just to remove test conversations.
