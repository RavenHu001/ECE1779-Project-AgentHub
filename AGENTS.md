# Working on AgentHub

Start with [the development handoff](docs/DEVELOPMENT_HANDOFF.md), then read the linked guide for the area you are changing. Inspect the current branch, working tree and recent commits before editing. Treat older design documents as proposals and verify implementation details against the code.

- Base new work on the repository's current integration branch and use a feature branch. Follow the current request for review and merge authority.
- Use Docker Compose and fixed reply mode for local checks and CI. Run backend tests against the isolated test database. Keep credentials and resolved Compose configuration private.
- Preserve API ownership checks, submission idempotency, conversation ordering and worker lease fencing. Coordinate schema changes with the team and add a new Alembic revision for changes to applied schemas.
- Keep reviewer-facing documentation and system-generated text in English. Use English and Chinese for source comments. Preserve user-provided Unicode.
- Report the revision and checks actually run. Distinguish offline client tests, local container smoke tests and live provider checks.
