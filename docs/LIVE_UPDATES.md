# Live updates (Server-Sent Events)

The browser receives conversation changes over a Server-Sent Events (SSE) stream, with polling as a fallback. The default stream uses message sequences. Clients can opt into durable replay of messages, approvals and execution events stored by migration `0007_conversation_events`.

## Endpoint

`GET /conversations/{conversation_id}/events` returns `text/event-stream`. The following cursor behavior applies to the default version.

- **Authentication and ownership.** The endpoint uses HTTP Basic, like every other endpoint. It checks ownership once before streaming. An unknown conversation, or one owned by another account, returns `404`. Missing or invalid credentials return `401`.
- **Resume cursor.** Send `Last-Event-ID: <seq>` with the last message sequence received. The stream replays later messages in order. Without the header, or with an invalid value, the stream starts at the conversation's current last message. The client loads earlier history through `GET /conversations/{id}/messages`.
- **Lifetime.** Each stream closes after 300 seconds so load balancers can rebalance connections. The client then reconnects with `Last-Event-ID`. The stream sends a `: keepalive` comment after 15 seconds without events.
- **Database use.** Every 0.5 seconds the stream reads committed rows in a short read-only session. It does not hold a database connection between reads. Tunables are module attributes in [events.py](../backend/app/events.py).

## Default stream events

| Event | `id` | `data` |
| :-- | :-- | :-- |
| `ready` | Current cursor | `{"cursor": <seq>}`. Sent once per connection. |
| `message` | Message `seq` | The same object as one row of `GET /conversations/{id}/messages`, including any saved `execution_id` and `proposal`. |
| `job` | None | Public job fields (`id`, `source_message_id`, `kind`, `status`, `attempts`, `last_error`, `created_at`, `finished_at`). Sent when a job's status or attempt count changes. Lease fields are never included. |
| `proposal` | None | The public proposal object. Sent when a proposal appears or its status or decision changes. |
| `activity` | None | Source message ID, initial job ID/status/attempts and safe tool-step labels. Sent when the saved activity snapshot changes. |
| `error` | None | `{"detail": "Database temporarily unavailable"}`. The stream then closes, and the client reconnects with backoff. |

Each connection tracks the state it has already sent, so it sends a job or proposal only when it changes. A reconnect resends current active jobs, so listeners should treat events as idempotent.

## Durable event replay

1. Read the first page of `GET /conversations/{id}/messages` with `after_seq=0` or omit that parameter. The body remains a message list. The `X-Conversation-Event-Cursor` response header captures the conversation's event head from the same database snapshot as that page. Use the last message's `seq` as `after_seq` to load subsequent pages. Each page is a separate read, and the v2 cursor is returned only with the first page.
2. Open `GET /conversations/{id}/events?version=2` with that header value as `Last-Event-ID`. On reconnect, send the ID of the last complete event frame. The server replays later events from PostgreSQL in sequence order. An explicit `v2:0` starts at the first recorded event.
3. Retain the cursor per conversation. Deduplicate replayed frames by conversation ID and event sequence. Each event describes a committed transition and carries references to the affected records. Read history to retrieve full messages and proposal details, and `GET /jobs/{id}` for job state. Use the source message's saved `execution_id` with `GET /executions/{execution_id}` to retrieve eligibility and the applied refund. [Execution details example](../README.md#read-a-saved-refund-result)

The version 2 endpoint returns `422` before streaming if the cursor is missing, malformed, a legacy numeric cursor, or ahead of the conversation's committed event head. Reload the history snapshot to obtain a valid cursor. Both versions check the same account ownership and use short database sessions.

Each stored frame uses an event name such as `proposal.decided` and an ID such as `v2:42`. Its JSON contains `seq` (a decimal string), `type`, `conversation_id`, `created_at`, and the original `payload`. Supported types are `message.created`, `proposal.created`, `proposal.decided`, `job.status_changed`, `resolution.completed`, `resolution.invalidated`, `agent.step_started`, and `agent.step_completed`. Agent events contain `job_id`, `source_message_id`, and a `step` with `seq`, `name`, `status`, and a fixed public `text` label. The connection confirmation, heartbeat, and error frames leave the cursor unchanged. Existing history remains available through the message endpoint. Durable replay covers events recorded after the migration and the installation of the event writers.

## Browser client

The browser reads the version 2 stream with authenticated `fetch` and a body reader. With the same account and conversation open in two browsers, a refund approved in one browser can finish after that browser closes. The other browser can see the saved result through its live updates or after reopening the conversation. A streaming HTTP client can verify the individual v2 events. [Browser implementation](../backend/app/static/chat.js) · [Cross-replica replay test](MANUAL_TESTING.md#mt20-resume-durable-sse-on-another-api-replica)

- **Starting and stopping.** Opening or creating a conversation reads the first history page for its snapshot cursor, then opens the stream with that cursor. Starting a new chat or signing out closes it.
- **Reconnection.** After a normal close, the client reconnects within 250 ms. After a failure, it backs off from 1 s to at most 15 s with jitter. A stream with no data for 45 seconds is treated as dropped. A rejected cursor (HTTP 422) triggers a fresh snapshot. A `401` or `404` stops the stream without retrying.
- **Removed chats.** A `conversation.deleted` event, or a `404` from either the snapshot request or the stream connection, clears that active chat, its pending submission and draft. It also releases the composer and refreshes the sidebar. The snapshot case covers a chat removed in another tab between the history read and the subscription. Late responses from the removed chat cannot overwrite a new draft. Responses from an obsolete stream cannot clear another conversation or account. A separate chat metadata mutation keeps its busy state until its own request completes.
- **Reauthentication.** A `401` from the event stream or a history refresh it triggered stops live updates, cancels scheduled refresh retries, and opens the sign-in dialog. The client compares the user ID returned by `GET /me` with the previous user's ID. The same account keeps its open conversation and draft and reconnects. A different account starts with an empty chat and loads its own conversation list. The previous account's messages, drafts, and pending submission state are cleared, and late responses from its workspace are ignored.
- **Using events.** `job.status_changed` events complete message sends and approved refund continuations immediately. Stored events and `ready` frames trigger one debounced history refresh, which reuses the existing stale-response guards. Stored `agent.step_started` and `agent.step_completed` events refresh the activity timeline. Activity is rendered once per request from the saved snapshot, rather than appended directly from events. Polling during a send also refreshes activity when SSE is unavailable.
- **Activity timeline.** Muted lines show tool progress, eligibility outcomes, retries, approval waiting and the final resolution state. Unfinished steps are never marked completed on failure. Ordinary chat has a working indicator but no invented tool steps. The final assistant answer stays separate. Reopening a conversation reconstructs the timeline from saved steps; a short tool call may finish between stream polls.
- **Fallback.** While no stream is live, or where the browser lacks body streams, the client polls `/jobs/{id}` as before. When a stream is live, it confirms a job over REST if no event arrives within 5 seconds.
- **Status.** The footer shows **Live updates** (green), **Reconnecting…** (amber) or **Local workspace**.

## Verification

Automated coverage:

- [test_event_store.py](../backend/tests/test_event_store.py) covers migration from `0006`, concurrent writes, ordered reads and rollback of business changes with the event batch.
- [test_events.py](../backend/tests/test_events.py) covers ownership and legacy replay, plus the durable snapshot cursor, cursor validation, ordered page replay, and approval committed between the snapshot reads.
- [test_resolution_workflow.py](../backend/tests/test_resolution_workflow.py) covers duplicate approval, worker takeover, lease expiration while waiting for the event head and atomic rollback.
- [test_chat_client.cjs](../backend/tests/test_chat_client.cjs) covers completing a send from a pushed job event without polling, parsing a frame split across chunks, reconnecting with `Last-Event-ID`, and clearing a removed chat after `404`. Deletion tests cover in-flight submissions with late successful or failed responses, both through pushed deletion and a reconnect after missing the event. It also verifies draft retention after signing back in to the same account and workspace isolation when a different account signs in during an in-flight send.

Manual check against the running stack, with credentials from your private `.env`:

```bash
curl -sN -u "$BOOTSTRAP_USERNAME:$BOOTSTRAP_PASSWORD" \
  http://127.0.0.1:8000/conversations/<conversation-id>/events
```

Send a message in the browser for that conversation. The terminal shows `message` and `job` frames as they are committed. In the browser, the footer shows **Live updates**. If you stop the API with `docker compose stop api`, the footer changes to **Reconnecting…**. After `docker compose start api`, the stream resumes, and messages saved during the outage appear without a page reload.
