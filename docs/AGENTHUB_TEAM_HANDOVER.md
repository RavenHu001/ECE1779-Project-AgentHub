# AgentHub: Team Architecture and Design Handover

Updated: October 7, 2026 (America/Toronto)

## 1. Purpose and status

This document gives teammates and their AI assistants the context needed to discuss and develop AgentHub. It consolidates the current proposal, diagrams, and subsequent design explanations. It is not subject to the proposal's 2,000-word limit.

We are in the exploratory and design phase. This is a proposed architecture, not a description of a completed application. The stack and implementation details can change if the team has a good reason. Do not infer deployed services, passing tests, team consensus on every detail, or completed experiments from future-tense plans.

Use the name **AgentHub**. The older `AgenHub` spelling is obsolete.

### How to interpret decisions

| Status | Meaning | Current examples |
| --- | --- | --- |
| Confirmed direction | Explicitly requested or retained in our discussion | Focused scope, basic chat, durable tasks, human approval, early CI/CD, simple design suitable for an inexperienced team |
| Current proposal | The working choice to use when planning; still revisable | Support/refund workflow, React, FastAPI, Python workers, PostgreSQL job queue, SSE, DigitalOcean and Swarm |
| Open implementation detail | Needs a concrete team decision or an experiment | Exact schemas, API routes, model, authentication mechanism, polling intervals, instance sizes, migration strategy |

The source proposal is `PROJECT_PROPOSAL.md`. This handover adds explanations and identifies implementation questions; it does not silently finalize them. If future code or team decisions disagree with these documents, reconcile the difference and update the relevant files.

## 2. Product and scope

AgentHub is an AI workspace for small businesses, inspired by the experience of giving an agent a goal and watching it use tools to complete the work. The current course project focuses on customer support and refund triage.

An employee can ask questions or request work through a conversation interface. The application answers ordinary questions, asks for clarification when needed, or starts a tracked task. Tasks gather information, prepare a resolution, and request human approval before applying business changes. Users can inspect progress and history after refreshing or returning later.

Example task:

> Review ticket 391, check the customer's order and refund policy, draft a reply, and prepare a refund if eligible.

The demonstration uses synthetic customers, tickets, and orders. Refunds and customer messages are simulated. We must clearly identify simulated actions rather than imply that a real payment or message was sent.

### Included in the current scope

- Basic text chat with saved conversations and bounded recent context.
- One agent loop and one support workflow.
- Three bounded tool capabilities: retrieve ticket/order details, check eligibility, and apply an approved resolution.
- Execution history, progress events, approval decisions, and audit records.
- Recovery from application or worker interruption.
- A simple interface for chat, tasks, history, and approvals.
- Required cloud deployment, persistence, replication, and monitoring.
- SSE, early CI/CD, external model integration, automated backups, and basic application security.

### Outside the current scope

Multiple agents, arbitrary tools or code execution, many SaaS integrations, a visual workflow builder, RAG/vector search, attachments, changing a running task through chat, automatic database failover, and autoscaling are not planned for the first version. MCP, LangGraph, and similar frameworks are not dependencies of the current design.

Multiple worker replicas do not mean multiple collaborating AI agents. They are copies of the same background program processing separate jobs or recovering abandoned work.

## 3. Architecture at a glance

```text
User / React interface
    | HTTPS: messages, history requests, approval decisions
    | SSE from API: replies, progress, approval requests
    v
FastAPI service <------> PostgreSQL <------> Python worker pool
                             |                    |
                             |                    +-- OpenAI API
                             |                    |
                             |                    +-- Simulated business service
                             |
                     Persistent database volume
```

The API and workers coordinate through PostgreSQL. The API does not need to keep a long-running task in the browser's request or call a particular worker directly. The browser does not talk directly to the database, model provider, or business service.

The model proposes answers and tool calls. Application code controls what actually executes.

### Component responsibilities

| Component | Responsibility |
| --- | --- |
| React interface | Login, conversation display, message submission, task progress/history, and approval or rejection controls |
| FastAPI service | Authenticate requests, enforce access, save messages and work requests, retrieve history, record approval decisions, deliver SSE |
| PostgreSQL | Durable users, conversations, messages, jobs, execution checkpoints, tool results, proposals, approvals, and events |
| Python worker | Claim work, load context, call the model, validate tool requests, run tools, save progress, pause, retry, and resume |
| OpenAI API | Generate chat replies, suggest permitted tools, interpret results, and draft responses |
| Simulated business service | Provide sample ticket/order data, deterministic eligibility checks, and simulated resolution operations with idempotency |


## 4. Chat and task processing

The current proposal queues each submitted message for the worker. Ordinary chat therefore also uses the existing background infrastructure; we have not proposed a separate chat service.

1. The API checks access, saves the message, and queues work in PostgreSQL.
2. A worker claims that work and loads a bounded amount of conversation context.
3. The model produces an answer, a clarification question, or a proposed tool call.
4. An answer or clarification is saved and displayed. A tool request becomes a tracked task linked to the originating conversation.
5. For a task, the worker validates each tool call, invokes the permitted operation, records results, and continues the loop.
6. A read-only task can finish with findings. A task that proposes business changes enters the approval flow.

Examples:

| Message | Expected handling |
| --- | --- |
| “What does refund eligibility mean?” | Direct chat answer; no business tool required |
| “Check whether order 391 qualifies.” | Tracked task that reads order/policy data; no refund is authorized |
| “Review ticket 391 and prepare a refund if eligible.” | Tracked task that may produce a proposal requiring approval |
| “Could we refund this?” without a clear order or intent | Clarify before assuming an action |

Routing can be part of the first model response. A separate classifier or second model is not required by this design. The exact structured response/tool schema is still open.

The interface should distinguish a proposed action from a completed action. The model must not report a successful refund merely because it generated a tool call; the application needs a recorded tool result.

Chat history is durable, but the model receives bounded context rather than the entire history indefinitely. Context limits and the policy for concurrent messages within one conversation remain to be specified. Editing running tasks through conversational messages is outside the first version.

## 5. Human approval

The current approval policy is one explicit approval for a specific proposed resolution, after gathering evidence and before applying business changes.

| Operation | Approval requirement |
| --- | --- |
| Read a ticket or order the user is authorized to access | No extra approval |
| Check eligibility or draft a reply | No extra approval |
| Save internal messages, checkpoints, and progress | No extra approval |
| Apply a simulated refund and associated ticket/reply changes | Explicit approval of that resolution |

Approving an early plan is not a substitute for approving the eventual action details. Requiring approval for every read would add interruptions without improving this workflow.

### Approval lifecycle

1. The worker prepares and saves a proposal containing the target order/ticket, refund amount if applicable, proposed reply, ticket changes, and supporting evidence.
2. The execution becomes `WAITING_FOR_APPROVAL`. Its state is durable; the worker is released to process other jobs.
3. The API delivers the approval request to the browser through SSE. The same proposal can also be loaded from history.
4. The user clicks Approve or Reject. The API checks the user's authority and records the decision against that specific proposal.
5. Approval makes the execution runnable again. A worker verifies the approval and applies the authorized resolution. Rejection ends the execution without applying it.
6. The outcome is saved and displayed.

The approval must be tied to stable action inputs or a proposal version. A material change, such as a different amount, target, or reply, requires a new approval. How to handle a policy/order changing while approval is pending needs an explicit implementation decision; do not silently execute a different action using the old approval.

The application enforces this boundary. A model response, inferred user intent, or ordinary chat message must not bypass the approval check. The planned UI uses explicit approval controls.

## 6. Durable jobs, recovery, and side effects

PostgreSQL serves as both the application database and the durable job queue. We currently do not plan a separate Redis, RabbitMQ, or Kafka service.

Workers claim jobs with time-limited ownership. If a worker stops responding, its claim expires and another worker can recover the work from saved state. Work is checkpointed at meaningful boundaries, including tool outcomes and approval pauses.

A candidate public execution lifecycle is:

```text
QUEUED -> RUNNING -> COMPLETED
             |
             +-> WAITING_FOR_APPROVAL -> QUEUED -> RUNNING -> COMPLETED
                         |
                         +-> REJECTED

Unrecoverable error / exhausted retries -> FAILED
Expired worker claim -> eligible for recovery
```

These are proposed state names, not a finalized schema. Job attempts, leases, and retry timing may be separate fields or records rather than user-facing states. Cancellation behavior is not yet specified.

### The critical retry case

An external action can succeed just before the worker crashes or loses the response. The replacement worker then cannot assume that the action failed.

The simulated business service must persist an idempotency key with the action result. A retry of the same action uses the same key and returns the existing result instead of applying another refund. A new worker attempt must not generate a new key for the same logical action.

Persisting a task in PostgreSQL does not by itself guarantee exactly-once external side effects. Our claim is recoverable execution with idempotent simulated actions, supported by tests.

### Details to settle before implementing recovery

- Atomic job claiming, lease duration/renewal, and conditional state updates.
- Preventing an old worker whose lease expired from overwriting a new worker's progress.
- Which failures are retryable, maximum attempts, timeouts, and retry delays.
- The checkpoint contents needed to continue an interrupted model/tool loop.
- How duplicate submissions and repeated approval clicks are handled.
- How the simulated service persists its results and idempotency records across its own restart.
- Whether applying a resolution is one transaction in the simulation or several separately recoverable actions. One UI approval does not automatically make several writes atomic.

These are design questions within the existing architecture, not a request to introduce a larger workflow platform.

## 7. History and real-time updates

The API reads saved conversation and execution data when a user opens a page. That is the “read history” part of the architecture diagram.

For live progress, the worker saves events such as “checking order,” “waiting for approval,” or “completed.” The API retrieves those events and forwards them to the browser through Server-Sent Events (SSE).

```text
Worker writes event -> PostgreSQL -> API reads event -> SSE -> Browser
```

The arrow from PostgreSQL to the API describes data returned from a query. It does not imply automatic database push notifications.

The simple implementation discussed is periodic querying for events newer than the last delivered sequence number. The exact polling interval is open. PostgreSQL notifications could be evaluated later, but are not required for the first version.

Events are persistent so an API restart or reconnection to a different replica does not lose the execution timeline. The browser resumes from its last event ID and should tolerate replayed events. Authorization applies to history queries and SSE streams as well as ordinary task requests.

SSE is used for server-to-browser updates. Messages and approval decisions use ordinary HTTPS requests. WebSockets are not currently necessary. Token-by-token model streaming is not a committed feature; delivering completed chat replies and task progress through SSE is sufficient for the current proposal.

## 8. Data and interface boundaries

The following are conceptual records, not final table names or API contracts:

| Concept | Minimum information it needs to connect |
| --- | --- |
| User/access | Identity and ownership or permission rules |
| Conversation/message | Conversation owner, message author, content, ordering, and timestamps |
| Job | Work to process, status, lease owner/expiry, attempts, and retry eligibility |
| Execution | Originating message, user, task status, and current checkpoint |
| Tool call/result | Execution, tool, validated arguments, outcome, and action key where relevant |
| Proposed resolution | Exact action inputs, evidence, and stable version or identifier |
| Approval | Proposal, deciding user, decision, and decision time |
| Progress/audit event | Conversation/execution, event sequence, type, and suitable display data |

Before parallel implementation, agree on message submission, history retrieval, execution details, approval decisions, SSE event envelopes, and tool input/output contracts. Exact routes and field names remain open.

The simulated business service is a distinct logical boundary. It does not require another database technology. Decide whether it uses separate PostgreSQL tables or a schema, and how access is restricted, before implementing it.

## 9. Deployment and operations

### Local development

Docker Compose runs the interface, API, worker, simulated business service, and PostgreSQL. Database data uses a persistent volume. Setup should provide sample data and safe configuration templates without committed credentials.

### Proposed cloud deployment

- Provider: DigitalOcean.
- Orchestrator: Docker Swarm on two Droplets, initially one manager and one worker node.
- API and Python worker services have replicas; Swarm handles API service routing/load balancing.
- PostgreSQL runs as a single instance pinned to the node with its attached DigitalOcean Volume.
- HTTPS entry, web serving, and certificate management are required, but the specific proxy and domain arrangement are still open.
- Replica counts, machine sizes, service placement, image registry, and budget remain to be finalized.

Application worker containers are different from Swarm worker nodes. A worker container is our Python process; a Swarm worker node is a machine that runs containers. Do not assume each has a one-to-one mapping.

This topology demonstrates replication and application recovery, not full system high availability. The single database has no automatic failover. Loss of the only Swarm manager prevents normal cluster management until recovery. An attached volume does not automatically provide database failover to the other node.

The API and workers communicate on private service networks with the database and simulated service. Their secrets remain server-side.

### CI/CD from the first development stage

The team explicitly moved CI/CD earlier so everyone can use a consistent deployment process from the beginning:

1. Pull requests run available tests and build container images.
2. Reviewed changes merged into the main branch publish versioned images.
3. The pipeline deploys the Swarm services, serializing deployments to prevent overlap.
4. A health check verifies the deployment; previous image versions remain available for rollback.

Start with a minimal application and expand tests as features arrive. Branch permissions, deployment credentials, image registry, database migration execution, and health-check criteria are open details. Rolling back an image does not automatically undo a database migration; migration compatibility needs a deliberate approach.

### Monitoring and backups

Use DigitalOcean monitoring for CPU, memory, and disk metrics with dashboards/alerts. Application logs should include execution IDs, durations, tool errors, and retry counts. Provider monitoring and application-level diagnostics serve different purposes.

A scheduled job creates a daily PostgreSQL backup, uploads it to private DigitalOcean Spaces storage, and retains seven daily copies. Verify recovery by restoring into a clean database and checking conversations, executions, approvals, and relevant simulated action records. The exact scheduler and backup tooling are open.

Daily backups allow recovery to a daily checkpoint, not zero data loss. A persistent volume protects data across container replacement; backups address a different failure case and need a restore test. Ensure only one scheduled backup job runs per intended interval even when services are replicated.

## 10. Security and model boundaries

The proposed security scope includes account login, hashed passwords, server-side authorization, HTTPS, Swarm secrets, and protected CI credentials. “Basic authentication” in our discussion means a simple account/login feature, not a commitment to the HTTP Basic authentication protocol.

Users must only access conversations, tasks, event streams, and approval actions they are permitted to use. The exact ownership model, session mechanism, and whether separate approver roles are needed remain open. A full multi-tenant organization system has not been specified.

The model can only propose allowed tool calls. Application code validates arguments, enforces deterministic eligibility and authorization rules, and checks approval before side effects. Ticket text and model output must not be treated as permission to bypass those checks.

Keep keys out of the repository, browser, and logs. Set model-call, context, duration, and cost limits. Choose the exact model/API details during implementation; no particular model has been selected.

## 11. Course alignment and evidence

Course: University of Toronto ECE1779, Cloud Computing, Fall 2026. The team is expected to have 2–4 students, but actual names, count, roles, and budget are not recorded yet.

Official references:

- [Project guidelines and proposal rubric](https://www.eecg.utoronto.ca/~cying/courses/ece1779-cloud/project/handout/)
- [Project overview](https://www.eecg.utoronto.ca/~cying/courses/ece1779-cloud/project/overview/)

The proposal was checked against the guidelines during this discussion. Recheck official sources and Quercus before submission; this handover is not a substitute for the handout.

Required technologies include Docker, Compose, PostgreSQL, implemented persistent storage, DigitalOcean or Fly.io, Swarm or Kubernetes, provider monitoring, and at least two advanced features. Our working choice is DigitalOcean with Swarm. AWS was an original idea but does not satisfy the stated provider requirement. Managed PostgreSQL alone does not demonstrate implementation of persistent storage.

SSE and CI/CD are our primary advanced-feature commitments. External model integration, backup/recovery, and security are also planned. We do not need to rely on whether the OpenAI integration alone is accepted as an advanced feature.

### Planned verification

| Area | Evidence to collect |
| --- | --- |
| Chat and routing | Direct answers, clarifications, and task creation behave correctly; no unauthorized action from routing |
| Business workflow | Eligible, ineligible, missing-information, and rejected-resolution cases |
| Approval | Exact proposal is authorized; changed inputs and unauthorized decisions are rejected |
| Recovery | Terminate a worker and show another resumes saved work |
| Idempotency | Stop after simulated action success but before checkpointing; retry does not duplicate the action |
| Persistence | Replace application/database containers and show expected data survives |
| SSE | Disconnect/reconnect, including another API replica, and recover missed events |
| Replication | Show requests served by multiple API replicas and work claimed without duplication |
| Backups | Restore into a clean database and verify records |
| CI/CD | Successful checks, deployment, health check, and image rollback exercise |
| Monitoring | Visible provider metrics and a demonstrated alert configuration |
| Performance | Compare one and two workers with a repeatable small workload; separate queue delay from execution time |

Use a deterministic model stub for repeatable integration and infrastructure tests. Run separate live model tests. Clearly label demo fallback mode; do not claim stub output came from the live model. No tests or experiments in this table are claimed as completed.

## 12. Tentative schedule and team coordination

| Period | Current planned milestone |
| --- | --- |
| October 8–18 | Confirm scope, stack, ownership, budget, contracts, and access; establish CI/CD and minimal cloud deployment; check persistence |
| October 19–November 1 | Integrate chat, task routing, approval, simulated resolution, checkpoints, and retries locally |
| November 2–7 | Deliver the workflow through CI/CD; test replication and rollback; finalize and submit proposal |
| November 8–17 | Complete SSE, security, monitoring, backups, and recovery/access tests |
| November 18–24 | Fix integration issues, collect results, rehearse live demo, submit introduction and slides |
| November 25–December 5 | Final fixes, clean setup verification, report, video, and AI interaction record |

Proposal deadline: November 7. Project introduction: November 23. Slides and presentation work cutoff: November 24. Presentations: November 25 or December 2. Final repository: December 5. These are the dates recorded in the reviewed course material, subject to official updates.

Work areas are application/data, agent execution, interface, and cloud/delivery. These are workstreams, not four confirmed team members. Names and assignments remain placeholders in the proposal. Agree on shared contracts early, use small reviewed changes, integrate a working flow weekly, and keep individual contributions traceable.

The proposal must remain a single Markdown file within 2,000 words and follow its four required sections. The separate handover can be longer. Preserve genuine team reasoning and representative AI interactions for the proposal and eventual `ai-session.md`.

The user confirmed that teammates participated in discussions about the stack, adapting an original AWS idea to course requirements, including/excluding components, and balancing usefulness against simplicity and time. Some discussions occurred alongside AI assistance. Do not invent a clean chronology in which every idea existed before AI, or claim experiments were performed when only discussion occurred.

## 13. Open decisions and next steps

1. Record actual team members, responsibilities, experience, cloud credits, and spending limit.
2. Confirm the working stack and support scenario through a minimal deployment and tool-flow experiment.
3. Agree on sample ticket/order data, eligibility rules, and exactly which changes constitute a resolution.
4. Define message/job/execution relationships and handling of multiple messages in a conversation.
5. Define approval versioning, rejection behavior, and stale proposals.
6. Specify claims, retries, checkpoints, duplicate prevention, and simulated-service persistence.
7. Finalize authentication, access ownership, SSE replay, and API/tool contracts.
8. Select model, context/call limits, cloud sizes, registry, HTTPS entry, migrations, and backup scheduler.
9. Implement the smallest complete path and validate it before broadening scope.

Favor choices the team can explain, operate, and test within the course timeline. Changes should have a stated reason and update the proposal, handover, and diagrams where relevant.


## 14. Context prompt for a teammate's AI assistant

> Read this handover and the latest PROJECT_PROPOSAL.md before suggesting or implementing changes. AgentHub is still in design. Treat confirmed scope, current proposals, open details, and implemented facts separately. Keep the solution practical for an inexperienced course team. The current plan is basic chat plus one support workflow using React, FastAPI, PostgreSQL-backed jobs, Python workers, an external model API, approval before business changes, SSE, and Docker Swarm on DigitalOcean. CI/CD starts early. Do not add frameworks, infrastructure services, integrations, or major features without explaining why the existing design is insufficient. 
