# Worker baseline

This milestone extends the existing FastAPI/PostgreSQL backend with optional model-backed text chat. It reuses the existing tables, submission endpoints, job states, image and migration setup. No schema migration is required.

## Run locally

Start Docker Desktop in Linux-container mode. On Windows, finish WSL 2 / Virtual Machine Platform installation and restart Windows if requested. Then, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
```

The script locates Docker even if this terminal has an old PATH, creates `.env` only if absent, builds and starts Compose, and checks the real API and independent worker. Host Python is not required. Open <http://127.0.0.1:8000/> (or the configured `API_PORT`) and sign in using the bootstrap credentials in `.env`. The browser test page supports saved conversations, job status, and replies. It is a lightweight development client; the team's main frontend can use the same API.

Default `REPLY_MODE=fixed` returns `Message received.` and makes **no model requests**. The page clearly labels this mode. PostgreSQL stores messages and jobs on the existing persistent volume. Never use `docker compose down -v` on data you want to retain.

## Enable live model chat

Privately edit the ignored `.env`:

```dotenv
REPLY_MODE=openai
OPENAI_API_KEY=YOUR_PRIVATE_KEY
OPENAI_MODEL=gpt-5.4-mini
MODEL_TIMEOUT_SECONDS=30
MODEL_MAX_OUTPUT_TOKENS=600
MODEL_CONTEXT_MESSAGES=20
MODEL_CONTEXT_CHARS=12000
```

Then run `docker compose up -d --build --wait` to recreate the configured services. Never paste a key in chat, commit it, or print resolved Compose configuration in a shared log. The key is passed only to the worker container. Setting `openai` mode without a key fails worker startup; it does not silently switch to a simulated answer.

The configured model must support the Responses API and `reasoning.effort=none`. Model selection stays configurable. Context has both a message-count limit and a character limit. An individual message exceeding the context character limit fails explicitly rather than silently truncating the request. The model sees the current message and recent earlier turns from that conversation only. No business tools, refunds, approvals, SSE or token streaming are implemented at this milestone. The system prompt explicitly forbids claiming business actions were performed.

These controls reduce spending but are **not a dollar-denominated spending cap**. Use the API account's billing controls as well. Do not run large workloads before reviewing usage. Recovered attempts may repeat a paid model request, although only the current claim can save its reply.

## Worker behavior

- An atomic PostgreSQL claim selects a ready or expired job with `FOR UPDATE SKIP LOCKED`.
- Jobs run in user-message order within each conversation; independent conversations can run on different replicas. A failed earlier job does not block later messages.
- Database sessions are closed before model calls. A heartbeat renews the lease in independent short transactions every one-third of the lease duration.
- Every completion, renewal and failure checks the current claim token and expiry. A stale worker cannot save an answer after another worker takes ownership.
- The worker uses the OpenAI Python SDK's Responses API with `store=False`, no tools, bounded output, and no SDK retries. PostgreSQL remains the application's source of conversation history.
- Connection errors, temporary rate limits and selected transient HTTP failures requeue the same job with exponential delay, bounded by `WORKER_MAX_ATTEMPTS` (default 3). Exhausted credits, invalid credentials, rejected requests, incomplete output and blank output fail visibly. Provider error bodies are not written to public errors or logs.
- The assistant message and job success are committed together using the existing unique reply constraint. Worker interruption can repeat a model call but cannot produce two saved replies for the same source message.
- `/health` reports database connectivity and configured reply mode. It is not a worker heartbeat or model-availability check. The smoke check exercises the actual worker.

## Verification

```bash
docker compose --profile test build test
docker compose run --rm --no-deps test sh -c 'ruff check . && ruff format --check .'
docker compose run --rm test
docker compose exec -T api python -m app.smoke
```

Tests use the existing isolated PostgreSQL test service. Added coverage exercises ordered context for queued messages, conversation isolation and limits, slow model calls with lease renewal, stale renewal rejection, persisted retry exhaustion, error redaction, and the actual SDK request/response contract through an in-memory HTTP transport. No API key or paid requests are required for CI.

To exercise replication, start `docker compose up -d --scale worker=2 --wait` and repeat the smoke check. To verify persistence, record the conversation UUID printed by the smoke check, recreate containers without deleting volumes, and use `python -m app.smoke --verify-conversation UUID` inside the API container.

## Next execution milestone

Agree with the team on tool-result/checkpoint records and exact proposal/approval contracts before adding simulated ticket/order reads and approved resolutions. Preserve the existing shared migration process. Cloud deployment will reuse the backend image with distinct API and worker startup commands; Swarm/Fly.io orchestration, secrets and persistent storage configuration remain separate deployment work.
