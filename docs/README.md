# AgentHub Documentation

The [repository README](../README.md) is the English entry point for reviewers and team members. It describes what currently runs, how to start it and how to exercise its API.

| Document | Purpose and status |
| --- | --- |
| [Development handoff](DEVELOPMENT_HANDOFF.md) | Fresh-machine setup, merged baseline, regression guarantees and next work |
| [Chat management](CHAT_MANAGEMENT.md) | Rename, pin and delete controls, API contracts and migration notes |
| [Agent execution completion plan](EXECUTION_COMPLETION_PLAN.md) | Review of main at `506bc66`, instructor feedback, remaining worker/model work and evaluation criteria |
| [Agent execution contract](AGENT_EXECUTION.md) | Implemented bounded model tools, checkpoints, stale approval checks, migration and SSE/API handoff |
| [Setup and team workflow](../README.md) | Current Docker services, accounts, shared access, API examples, tests and migration workflow |
| [Code structure](CODE_STRUCTURE.md) | A concise map of services, source modules, migrations, tests and the message flow |
| [Technology stack](TECH_STACK.md) | Current languages, libraries, versions, responsibilities and selection rationale |
| [Backend database reference](BACKEND_DATABASE.md) | Current chat and refund check tables, constraints and transaction boundaries |
| [Manual testing guide](MANUAL_TESTING.md) | Chat and refund workflow checks with expected outcomes and a test result record template |
| [Architecture and team handover](AGENTHUB_TEAM_HANDOVER.md) | Original English architecture proposal; planned features are not claims of implementation |
| [Developer documentation](developer/README.md) | Index of Chinese team explanations and historical specification drafts |

The current implementation provides durable chat submission, a simulated refund workflow with human approval in fixed mode, and opt-in OpenAI text chat. See [Worker baseline](WORKER_BASELINE.md) for setup, limits and verification. [Live updates](LIVE_UPDATES.md) stream conversation changes over SSE. See [cloud delivery](CLOUD_DEPLOYMENT.md) and [cloud operations](CLOUD_OPERATIONS.md) for production setup and verification.

Reviewer-facing documentation and system-generated runtime text are English. Source comments use English and Chinese. Chinese prose documents belong under `docs/developer/zh-CN/`; user-submitted Unicode content is preserved.
