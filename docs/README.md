# AgentHub Documentation

The [repository README](../README.md) is the English entry point for reviewers and team members. It describes what currently runs, how to start it and how to exercise its API.

| Document | Purpose and status |
| --- | --- |
| [Setup and team workflow](../README.md) | Current Docker services, accounts, shared access, API examples, tests and migration workflow |
| [Code structure](CODE_STRUCTURE.md) | A concise map of services, source modules, migrations, tests and the message flow |
| [Backend database reference](BACKEND_DATABASE.md) | Current four-table schema, relationships, constraints and transaction boundaries |
| [Manual testing guide](MANUAL_TESTING.md) | Fifteen manual checks with expected outcomes and a test result record template |
| [Architecture and team handover](AGENTHUB_TEAM_HANDOVER.md) | Original English architecture proposal; planned features are not claims of implementation |
| [Developer documentation](developer/README.md) | Index of Chinese team explanations and historical specification drafts |

The current implementation provides durable chat submission and fixed English replies. Model integration, approvals, SSE, simulated business operations and production cloud delivery remain planned work.

Reviewer-facing documentation and system-generated runtime text are English. Source comments use English and Chinese. Chinese prose documents belong under `docs/developer/zh-CN/`; user-submitted Unicode content is preserved.
