# Changelog

## Unreleased

Developed by **Jinxi Hu**. [PR #3](https://github.com/RavenHu001/ECE1779-Project-AgentHub/pull/3)

### 2026-11-08

- Documented the design and acceptance cases for the read-only refund check. [Developer guide](docs/developer/zh-CN/AGENTHUB_READ_ONLY_REFUND_CHECK.md)
- In fixed reply mode, users can ask whether a simulated order is eligible for a refund. The worker checks order ownership and returns the outcome, reasons, and eligible amount.
- Alembic revision `0003_read_only_refund_check` adds the `orders` table for simulated orders and their assigned accounts, and the `executions` table for structured eligibility results. The worker saves one result per source message in the same transaction as the chat reply and job completion. [Database reference](docs/BACKEND_DATABASE.md#read-only-refund-data)
- The `seed-demo-orders` admin command creates sample orders for eligible, expired, incomplete, and cross-account test cases. [Demo setup](docs/WORKER_BASELINE.md#read-only-refund-check)

### 2026-11-09

- Development accounts accept any non-empty ASCII password, including a single character.
- Dragging to select text in the sign-in form and releasing over the backdrop keeps the dialog open. The close button and Escape still dismiss it.
