# Worker baseline

The worker processes durable chat jobs with fixed replies or optional model text generation. Fixed mode also supports a read-only refund check backed by simulated orders in PostgreSQL.

## Run locally

Start Docker Desktop in Linux-container mode. On Windows, finish WSL 2 / Virtual Machine Platform installation and restart Windows if requested. Then, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_local.ps1
```

The script locates Docker even if this terminal has an old PATH, creates `.env` only if absent, builds and starts Compose, and checks the real API and independent worker. Host Python is not required. Open <http://127.0.0.1:8000/> (or the configured `API_PORT`) and sign in using the bootstrap credentials in `.env`. The browser test page supports saved conversations, job status, and replies. It is a lightweight development client; the team's main frontend can use the same API.

Default `REPLY_MODE=fixed` returns `Message received.` for ordinary chat and makes **no model requests**. The page labels this mode as Demo mode. PostgreSQL stores messages and jobs on the existing persistent volume. Never use `docker compose down -v` on data you want to retain.

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

The configured model must support the Responses API and `reasoning.effort=none`. Model selection stays configurable. In model mode, context has both a message-count limit and a character limit. An individual message exceeding the context character limit fails explicitly rather than silently truncating the request. Fixed mode does not load model context and preserves the API's 20,000-character message limit. The browser composer retains its 12,000-character limit for compatibility with the default model settings. The model sees the current message and recent earlier turns from that conversation only. The refund demo runs in fixed mode using the application policy.

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

Run browser state regressions on the host with Node.js 22 or newer; Node is needed only for these tests, not to run the application:

```bash
node --test backend/tests/test_chat_client.cjs
```

These tests execute the real client script with an in-memory DOM and controllable HTTP responses. They cover Unicode-safe generated titles, retaining drafts and submission keys across navigation, clearing private drafts on sign-out, and ignoring superseded history reads and errors. They do not replace visual and keyboard checks in a real browser. CI runs them before the container checks.

```bash
docker compose --profile test build test
docker compose run --rm --no-deps test sh -c 'ruff check . && ruff format --check .'
docker compose run --rm test
docker compose exec -T api python -m app.smoke
```

Tests use the existing isolated PostgreSQL test service. Added coverage exercises fixed replies across the full API message limit, ordered context for queued messages, conversation isolation and limits, slow model calls with lease renewal, stale renewal rejection, persisted retry exhaustion, error redaction, and the actual SDK request/response contract through an in-memory HTTP transport. No API key or paid requests are required for CI.

To exercise replication, start `docker compose up -d --scale worker=2 --wait` and repeat the smoke check. To verify persistence, record the conversation UUID printed by the smoke check, recreate containers without deleting volumes, and use `python -m app.smoke --verify-conversation UUID` inside the API container.

## Read-only refund check

Run the stack with `REPLY_MODE=fixed`. Create two active development accounts, for example `alice` and `bob`, using [the account commands](../README.md#individual-accounts). After migrations complete, seed the four demo orders.

```bash
docker compose run --rm admin seed-demo-orders alice bob
```

Sign in as `alice` and send `Can order 1001 be refunded?` or `订单 1001 能退款吗？`. The worker checks only orders assigned to that account. It saves the eligibility result, chat reply and job success in one transaction. The chat page displays the saved reply through the existing history endpoint.

### Acceptance checks

| Check as `alice` | Expected result after a fresh seed |
| --- | --- |
| Check order 1001, delivered five days ago | Eligible for 129.99 CAD |
| Check order 1002, delivered 45 days ago | Ineligible because the window expired |
| Check order 1003, missing its delivery time | More information required |
| Check order 1005 as a different user | Same response as for a missing order |

Use Docker Compose with fixed reply mode and the isolated PostgreSQL test database. Cover the exact 30-day boundary, ordinary chat, submission retries and worker lease expiry. A repeated submission or a replacement worker should leave one saved result and one reply for the source message. Verify that reopening the conversation displays the saved reply.

See the [developer implementation guide](developer/zh-CN/AGENTHUB_READ_ONLY_REFUND_CHECK.md) for the data fields, request flow, worker transaction and tests.

## Read-only ticket lookup

In fixed mode, seed the linked tickets and orders after creating the two active demo accounts.

```bash
docker compose run --rm admin seed-demo-tickets alice bob
```

Sign in as `alice` and send `Look up ticket 391` or `查询工单 391`. The reply includes the ticket status and the refund eligibility of the linked order. Tickets `392` and `393` exercise the expired window and missing delivery date. Ticket `395` belongs to Bob and returns the same response as an unknown ticket when Alice asks for it. A visible ticket with a missing or inaccessible order reports that more information is required.

The seed command can be repeated. It preserves existing order facts and ticket status and rejects conflicting ownership or links. A successful linked-order lookup saves its eligibility result with the chat reply and job success. Ticket lookup leaves business records unchanged.

## Prepare a refund proposal

With the same fixed-mode demo data, send `Prepare a refund for ticket 391` or `处理工单 391`. The worker checks the ticket, linked order, and refund policy, then saves a **Waiting for approval** card with the amount, proposed customer reply, and proposed ticket status. Refresh the page, sign in again, and open the saved conversation to see the same proposal. Ticket `392` returns the expired-window explanation and ticket `393` reports the missing delivery date.

The conversation history API returns the proposal on its originating message. The planning job finishes when it saves the proposal, and the execution remains waiting for approval.

## Approve or reject a refund proposal

Open the pending card and click **Reject** to test the rejection path. The card displays **Rejected**, and ticket `391` stays open. Prepare another proposal and click **Approve**. The card progresses to **Completed**, and the conversation receives a final reply confirming the simulated refund and ticket resolution. Refresh, sign in again, and open the conversation to verify the same decision and result.

The decision endpoint is `POST /proposals/{proposal_id}/decision`. Send `{"version":1,"decision":"APPROVE"}` or `{"version":1,"decision":"REJECT"}`. Repeating the same decision returns the original receipt. A conflicting decision or stale proposal returns HTTP `409`. A proposal for an order whose facts changed becomes invalid. The refund, simulated customer reply, ticket update, and worker result commit in one PostgreSQL transaction. A second proposal for the same order cannot refund it again.

The conversation pauses while its proposal is pending or its approved continuation is running. Other conversations can continue. Expired pending proposals are closed by the worker. When rolling out migration `0006`, replace planning-only workers before enabling approval requests so those workers cannot claim continuation jobs.
