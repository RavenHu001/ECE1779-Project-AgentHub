# AgentHub development handoff

This is the starting point for a new teammate or Codex session with a clean clone. It captures the baseline after [PR #1](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/1) merged into `main` on November 4, 2026 in Toronto. The merge commit is `56cf56a`, including the final regression fixes in `f901399`. Check recent history for changes since this snapshot.

## Start on another computer

Clone the repository, or fetch if you already have a checkout. GitHub access belongs to the GitHub identity used on that computer. Sign in to an authorized GitHub account for pushes and pull requests. A Codex account change does not carry GitHub credentials, local settings, database volumes or chat history to the new machine.

```bash
git clone https://github.com/RavenHu001/ECE1779-Project-AgentHub.git
cd ECE1779-Project-AgentHub
git fetch origin
git remote show origin
git status --short --branch
git log -8 --oneline origin/main
```

Start subsequent work on a branch from current `origin/main`, for example `git switch -c codex/<task-name> origin/main` with a real task name. Check for local edits before switching. The [README](../README.md#team-workflow-and-shared-access) covers shared development environments, individual accounts and migration coordination.

On Windows, start Docker Desktop with its Linux engine and WSL 2. Finish Windows feature setup and restart if prompted. From the clone root run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
```

The script finds Docker, initializes an absent `.env`, starts the stack and runs a smoke check. On Linux or macOS, use a running Docker engine, Compose v2 with `--wait`, and Python 3.10 or newer for initialization:

```bash
python3 scripts/init_env.py
docker compose config --quiet
docker compose up -d --build --wait
docker compose exec -T api python -m app.smoke
```

Open the [chat workspace](http://127.0.0.1:8000/) and sign in with `BOOTSTRAP_USERNAME` and `BOOTSTRAP_PASSWORD` from your ignored `.env`. The default username is `developer`. A new clone gets fresh local credentials and a fresh database. If a Codex browser permission prevents automatic opening, open the URL manually. The [API docs](http://127.0.0.1:8000/docs) and [health endpoint](http://127.0.0.1:8000/health) are also available. Adjust the URLs if you changed `API_PORT`.

Use `REPLY_MODE=fixed` for ordinary development. An ordinary chat message receives `Message received.` with no model charges. For a live model check, configure the key privately in `.env` and use the [model setup guide](WORKER_BASELINE.md#enable-live-model-chat). The worker uses the OpenAI Responses API with bounded input, output, timeouts and retry attempts. Live access and billing depend on the account supplying the key.

The `migrate` and `bootstrap` services exit successfully after startup. Use `docker compose ps -a` and `docker compose logs --tail 100 api worker migrate bootstrap` to diagnose failures. Rebuild after source changes because Compose runs code copied into the images. Ordinary `docker compose down` preserves the PostgreSQL volume. The `-v` option deletes it.

## Current architecture

AgentHub is a University of Toronto ECE1779 team project for small-business support work. The running baseline provides authenticated text chat and durable background execution.

| Component | Source and responsibility |
| --- | --- |
| Browser workspace | [Static assets](../backend/app/static/) served by FastAPI. The client renders chat, signs in, polls job status and retrieves history. |
| API | [main.py](../backend/app/main.py) and [schemas.py](../backend/app/schemas.py). Authenticate and scope access, accept messages, expose saved conversations and job status. |
| PostgreSQL | [models.py](../backend/app/models.py) and [Alembic revisions](../backend/migrations/versions/). Store users, conversations, messages, jobs, demo orders and eligibility results. |
| Independent worker | [worker.py](../backend/app/worker.py). Claim jobs, renew leases, retry transient failures and commit replies. |
| Reply providers | [replies.py](../backend/app/replies.py). Fixed acknowledgements or optional OpenAI text generation. |
| Runtime and CI | [Compose](../compose.yaml) runs API, worker, PostgreSQL, migration and bootstrap services. [GitHub Actions](../.github/workflows/ci.yml) tests the project. |

A message submission saves the user message and queues its job atomically. Retrying the same content with the same `submission_key` in that conversation returns the existing receipt. The worker claims ready jobs with PostgreSQL row locks and skips rows held by another worker. It processes turns in order within a conversation while allowing different conversations to run in parallel. During model calls it renews its lease in a separate transaction. Completion checks the current lease token and saves the assistant reply and success state together. Expired work can be reclaimed, and stale workers cannot commit replies. See the [database reference](BACKEND_DATABASE.md) and [worker guide](WORKER_BASELINE.md) for details.

## Behaviors to preserve from the merged PR

- **Message limits.** Fixed mode skips model context loading and accepts the API's full 20,000-character message limit. Model mode rejects an individual message that exceeds its configured context budget. The browser composer limits input to 12,000 characters to fit the default model configuration.
- **Unicode titles.** Generated conversation titles truncate by Unicode code point so they do not split surrogate pairs. User text stays Unicode.
- **Drafts and retries.** Switching saved conversations retains each draft with its uncertain submission key in page memory. Returning to a conversation restores both. Sign-out clears private drafts and credentials. Theme preference persists separately.
- **History races.** The client ignores obsolete history responses and errors after navigation or a newer read. Keep this guard when changing polling or navigation.
- **Credentials and model requests.** The browser uses development HTTP Basic authentication and keeps credentials in memory. Only the worker receives the model key. The job queue owns retries, and application logs and user errors omit raw provider error bodies.

## Validate changes

```bash
node --test backend/tests/test_chat_client.cjs
docker compose --profile test build test
docker compose run --rm --no-deps test sh -c 'ruff check . && ruff format --check .'
docker compose run --rm test
docker compose exec -T api python -m app.smoke
```

Node 22 or newer is for the offline client tests. Compose tests use the disposable `test-db` service. The smoke check uses the running API and worker and verifies that duplicate submission yields one saved reply. PR #1's final revision passed CI with the browser-state checks, container tests and live stack smoke test. Its final change records 39 backend tests and 8 browser-state tests. Use the [manual test guide](MANUAL_TESTING.md) for browser and API checks, and report fresh results for your revision.

To verify persistence, save the conversation UUID printed by the smoke check, recreate the stack while retaining volumes, and run `docker compose exec -T api python -m app.smoke --verify-conversation UUID` with that ID. To exercise parallel execution, use `docker compose up -d --scale worker=2 --wait` and submit work in separate conversations.

## Project direction

The proposed course demo is a single support and refund triage workflow with synthetic tickets and orders. The agent gathers facts, checks eligibility and prepares a resolution. Applying a simulated business change requires human approval of the specific proposal.

The [read-only refund check](WORKER_BASELINE.md#read-only-refund-check) evaluates one simulated order and persists the conclusion alongside the chat reply. Ticket lookup and approved resolutions follow the order check.

The [read-only ticket lookup](WORKER_BASELINE.md#read-only-ticket-lookup) resolves a ticket and its linked order for the signed-in account. Approval, SSE, cloud orchestration, deployment monitoring and automated backups remain planned. The [original architecture handover](AGENTHUB_TEAM_HANDOVER.md) explains the rationale and alternatives. Its React and cloud diagrams describe proposals. Use current code and team decisions to resolve differences.

For a fresh Codex chat, a useful starting prompt is:

> Read AGENTS.md and docs/DEVELOPMENT_HANDOFF.md. Inspect the current branch, git status and recent main commits. Use the handoff to identify the relevant code and verification commands for this task: [describe the work]. Preserve the existing worker and browser guarantees unless the task changes them.
