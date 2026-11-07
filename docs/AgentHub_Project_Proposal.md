# AgentHub Project Proposal

ECE1779 Introduction to Cloud Computing

## Motivation

Small-business support staff often handle refund requests by switching between tickets, orders, policies, and messaging tools. This process is time-consuming and difficult to track when work is interrupted.

AgentHub is an AI-assisted workspace that combines these steps into one workflow. Employees can request ticket reviews, eligibility checks, and proposed resolutions. The system gathers evidence and requires human approval before applying changes. Users can revisit the task history and outcomes after interruptions.

Our target users are small-business employees and owners. The project uses synthetic customer data and simulated refunds, focusing on reliable, auditable agent execution rather than a general-purpose AI platform.

## Objectives, Architecture, and Key Features

The main objective of AgentHub is to build a **stateful cloud application** that supports basic text chat and completes one support workflow reliably. Users will ask questions or request tasks through a conversation interface, follow progress, approve or reject resolutions, and revisit history. Work should survive browser disconnects and application restarts without repeating completed refunds. The proposed stack is a small React interface, a Python/FastAPI backend, PostgreSQL, and a Python worker. The backend and worker will share code but run separately, keeping long tasks independent of browser requests.

**Core Workflow:** We will support one agent and three bounded tools: retrieve ticket and order details, check refund eligibility, and apply an approved resolution. The model will choose tools and interpret their results, while application code will enforce eligibility rules and approval checks. A resolution will include the proposed reply and any simulated refund, so users can review the exact changes before approving them. The components will work together as follows:

1. The API saves each message in PostgreSQL and queues it for the worker. Using recent conversation history, the model answers directly, asks for clarification, or requests a permitted tool.

2. Tool requests become tracked task executions linked to the conversation. The worker validates calls and saves results and progress. Ordinary chat does not invoke tools or change business records.

3. Before applying a resolution, the worker saves its exact details and pauses for approval. Routing a message into a task never bypasses this check. Rejection ends the task without changes.

4. Approval makes the task runnable again. A worker applies the resolution and records its outcome. The browser receives replies and progress through SSE and can reload saved history.

**Core Cloud Components:**

* **State Management and Recovery:** PostgreSQL will store users, conversations, messages, executions, tool results, approvals, and audit events. It will also handle job claiming, avoiding a separate message broker. Claims expire if workers stop responding. Workers will save checkpoints and use bounded retries. Each approved resolution will use a stable idempotency key. The simulated service will save its business changes and result in one PostgreSQL transaction, with a unique constraint on that key. Retries will return the saved result.

* **Containerization and Local Development:** We plan to containerize the interface, API, worker, simulated service, and database with Docker and run them locally through Docker Compose.

* **Orchestration:** We will deploy replicated API and worker services on two DigitalOcean Droplets using Docker Swarm, chosen over Kubernetes for its simpler configuration and built-in load balancing.

* **Persistent Storage:** PostgreSQL will run on a designated node with an attached DigitalOcean Volume and a Swarm placement constraint. Data will survive container replacement, but automatic database failover is outside scope. The single Swarm manager is also a point of failure for cluster management.

* **Monitoring and Observability:** DigitalOcean metrics and alerts will monitor CPU, memory, and disk usage. Structured logs will track execution IDs, errors, retries, and completion times.

**Advanced Features:** Real-time functionality and CI/CD are our primary commitments and satisfy the two-feature minimum independently of the other features below.

* **Real-Time Functionality:** Server-Sent Events (SSE) will deliver execution progress and approval requests. Approval commands will use ordinary HTTP requests. Since updates mainly travel from server to browser, we do not need bidirectional WebSockets. Events will be persisted with sequence numbers, allowing reconnection through any API replica and replay of missed updates.

* **CI/CD Pipeline:** GitHub Actions will test pull requests, publish versioned Docker images, and deploy approved changes to Swarm one deployment at a time. Deployments will run one migration step before updating services, with schema changes compatible with both current and previous API and worker images. Health checks, rolling updates, and rollback support will help ensure reliable deployments.

* **External-Service Integration:** The worker will use the OpenAI API for chat, tool selection, and drafting, with timeouts, limits, and error handling.

* **Automated Backup and Recovery:** Daily PostgreSQL backups will be uploaded to private DigitalOcean Spaces, with seven copies retained. We will test restoration into a clean database.

* **Security Enhancements:** We will implement account login, hashed passwords, server-side checks on access to tasks and approvals, and HTTPS. Production credentials will be supplied through Swarm secrets and protected CI settings. API keys will stay on the server and outside the repository.

**Scope and Testing:** We will use a small agent loop and limit the interface to text chat, tasks, history, and approvals. Editing running tasks through chat, file attachments, multiple agents, workflow builders, RAG, arbitrary tools, and additional workflows are outside scope. We will test chat, ambiguous requests, and eligible, ineligible, incomplete-information, and rejected-resolution cases. Integration tests will check routing, conversation access, approval enforcement, worker recovery, and duplicate-action prevention across concurrent retries and simulated-service restarts. Deployment tests will verify multi-node replication, load balancing, rolling updates, data persistence, SSE reconnection, monitoring alerts, and backup restoration. We will compare one and two workers under a repeatable workload, reporting queue delay and completion time separately. A deterministic model stub will support these tests; live API runs will verify model integration separately.

## Project Plan and Team Responsibilities

We will work toward a complete demonstration by November 24, leaving the remaining time before December 5 for fixes, documentation, and final verification. Each period below lists the planned work and the evidence that will show it is complete.

| Period | Work and completion evidence |
| :---- | :---- |
| October 8 to 18 | Finalize architecture, database schema, and API contracts. Set up CI, Docker Compose, and initial deployment. |
| October 19 to November 1 | Implement chat, task routing, approvals, and simulated refunds. Test checkpoints and retries. |
| November 2 to 7 | Integrate the core workflow, test Swarm replication and rollback, and submit the proposal. |
| November 8 to 17 | Complete SSE, authentication, monitoring, and backups. Test recovery, reconnection, and restoration. |
| November 18 to 24 | Fix integration issues, evaluate performance, and rehearse the presentation. |
| November 25 to December 5 | Finalize testing, README, demo video, AI records, and submission. |

**Risk Management:** We will check cloud access, storage deployment, model API access, and the execution state model in the first stage. If integration takes longer than expected, we will simplify the interface while preserving the workflow, required cloud components, and primary advanced features. A clearly labelled deterministic demo mode will support demonstrations during model outages. We will set a cloud budget and per-task model limits before deployment.

### Proposed Division of Labour

Each area will have a primary lead, as listed below. All team members will review one another's work and may contribute to areas led by other members.

| Area | Lead | Responsibility |
| :---- | :---- | :---- |
| Application and data | Jinxi Hu | API, conversation storage, schema, login, access checks, approvals, event storage, and authenticated SSE endpoints |
| Agent execution | Shawn Chen | Worker, chat/task routing, model integration, tools, progress events, retries, and recovery tests |
| User interface | Chengxi Xiong | Chat, task progress, history, approval interface, and SSE updates, reconnection, and replay handling |
| Cloud and delivery | Yuzhe Zou | Containers, Swarm, storage, CI/CD, monitoring, and backup restoration |

Before developing components separately, we will agree on request formats, execution states, event formats, and sample data. We will integrate through small pull requests, run shared tests in CI, and review a working flow weekly, prioritizing the core workflow. Early CI/CD and a minimal Swarm deployment will give the team a common delivery process. GitHub issues and commits will track contributions; each lead will document decisions, reviews, and verification evidence for their area.

## Initial Team Reasoning and AI-Assisted Exploration

### Initial Team Reasoning

Our initial idea was an AI workspace that could help small businesses complete tasks across services while keeping users in control of important actions. We pictured separate layers for the user interface, API, task execution, and persistent storage, but had not settled the technology stack or how these components would interact. We initially considered AWS because it was the cloud provider most familiar to us.

During these early discussions, team members raised two questions: how could long-running tasks recover saved progress after a failure, and how could retries avoid repeating actions that had already succeeded? These concerns applied to business workflows generally. They later became concrete requirements for the refund workflow.

### Exploration and Evaluation

Reading the course requirements led us to reconsider AWS and choose DigitalOcean with Docker Swarm. Swarm meets the replication requirement with less configuration for our team to learn than Kubernetes. We refined the layered design into a separate API and worker so long tasks would not depend on browser requests.

We used ChatGPT to explore architecture, integrations, and task queuing, and to improve the proposal's wording.

AI initially suggested multiple connectors to e-commerce, CRM, and office applications. We judged that integration and testing work would exceed our project timeline and team capacity, so we narrowed the project to one refund workflow using synthetic ticket and order data and simulated business services.

AI also suggested a separate queue service. Given our small expected workload, we chose PostgreSQL-backed job claiming to avoid operating another system. We retained durable queuing while reducing deployment complexity; we did not need the scale of a dedicated messaging platform.

The recovery design had to address a worker stopping after a refund succeeds but before it records completion. Saving worker progress alone would not prevent a repeated refund. We therefore plan to keep the business changes and uniquely keyed result together in the simulated service, so another worker can safely retry the same resolution.
