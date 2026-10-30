# Current Backend Technology Stack

The implemented backend uses **Python, FastAPI, PostgreSQL and Docker Compose**. It accepts authenticated messages, saves durable jobs and produces a fixed English reply through a separate worker. The selection rationale below explains how each technology fits this initial implementation.

For source responsibilities, see the [code structure guide](CODE_STRUCTURE.md). For startup commands, see the [team guide](../README.md). A [Chinese version](developer/zh-CN/TECH_STACK.md) is available for developers.

## Application and database

| Technology and configured version | Role in this project | Why it fits |
| --- | --- | --- |
| Python 3.13 | Main language for API, worker, account commands and tests | One language lets the team reuse models, configuration and database code across processes |
| FastAPI 0.162.0 | Defines HTTP endpoints, authentication dependencies and the OpenAPI contract | Typed endpoints and generated API documentation make frontend coordination and manual testing easier |
| Uvicorn 0.54.0 | ASGI server that runs the FastAPI application | Provides an explicit server entry point that works inside the shared runtime image |
| Pydantic 2.14.2 | Validates input and defines response schemas | Centralizes checks such as UUID format and nonblank text, while controlling which fields the API returns |
| PostgreSQL 17 | Stores accounts, conversations, messages and jobs | Transactions, constraints and row locks support durable chat and coordinated worker claims in one database |
| SQLAlchemy 2.0.55 | Maps tables to Python classes and manages queries, sessions and transactions | API and worker can share data definitions and explicit transaction boundaries |
| psycopg 3.3.6 with psycopg-binary 3.3.6 | PostgreSQL driver used beneath SQLAlchemy | Connects Python to PostgreSQL; the binary package avoids building the driver from source in this image |
| Alembic 1.20.1 | Applies versioned database schema changes | Committed migrations let teammates and integration environments upgrade through the same revisions |

FastAPI organizes the request; Uvicorn serves it. Pydantic validates API data, SQLAlchemy organizes database operations, and psycopg communicates with PostgreSQL. These are complementary layers.

The relevant files are [main.py](../backend/app/main.py), [schemas.py](../backend/app/schemas.py), [models.py](../backend/app/models.py), [db.py](../backend/app/db.py) and [migrations](../backend/migrations/).

## Background processing and authentication

| Choice | Role | Why it fits and current limits |
| --- | --- | --- |
| Independent Python worker with a PostgreSQL job queue | Claims work, saves replies and records completion or failure | Reuses existing Python and database infrastructure. Claims use row locks, expiry times and fresh tokens so work can be recovered after interruption. No separate broker is needed for this milestone |
| HTTP Basic | Authenticates development accounts on API requests | Works with Swagger UI and Postman and supports individual accounts before a full login/session flow is built. Use SSH tunneling or HTTPS; Basic authentication does not encrypt credentials |
| scrypt with random salts | Hashes account passwords through Python's standard library | Stores password hashes rather than plaintext and avoids an additional hashing package in this initial image |

The worker currently produces `Message received.`. Long-running lease renewal and external business actions are not implemented. HTTP Basic is the current development mechanism; it is not a commitment to the final login design.

See [worker.py](../backend/app/worker.py), [security.py](../backend/app/security.py) and [manage.py](../backend/app/manage.py).

## Containers and team delivery

| Technology | Role | Why it fits |
| --- | --- | --- |
| Docker | Builds the runtime image and a separate test image | Packages the Python environment with the code so teammates and CI can run the same application setup |
| Docker Compose | Connects API, worker, PostgreSQL, migration and account initialization services | Defines startup order, networks, volumes and isolated project names in one shared file, usable on a developer machine or a shared server |
| Git | Tracks source, migrations, dependency files and documentation | Branches and reviewed commits let contributors coordinate changes |
| GitHub Actions | Builds containers, checks code, runs database tests and verifies the live service path | Gives pull requests a shared verification process before integration; remote deployment is not configured yet |
| uv and pip | uv generates resolved dependency files; pip installs them while building images | Maintainers can resolve versions once and Docker installs the reviewed list. uv is a maintenance tool, not a required runtime service |

The project requires a running Docker Engine and Compose v2 or newer with `--wait` support. Host Docker and uv versions are not pinned by the application. The images use `python:3.13-slim-bookworm` and `postgres:17-bookworm`; these tags select version families rather than immutable image digests.

Configuration lives in [compose.yaml](../compose.yaml), the [Dockerfile](../backend/Dockerfile), [.env.example](../.env.example) and the [CI workflow](../.github/workflows/ci.yml). Real environment credentials stay in ignored `.env` files.

## Testing and code quality

| Technology and locked version | Role | Why it fits |
| --- | --- | --- |
| pytest 9.2.0 | Runs integration tests and shared fixtures | Provides repeatable cases for request handling, ownership, transactional rollback and worker recovery |
| HTTPX 0.28.1 | Supports FastAPI's HTTP test client | Exercises request/response behavior and authentication in automated tests |
| Ruff 0.17.3 | Checks common code problems, imports and formatting | One shared configuration reduces style differences across contributors and CI |
| Swagger UI through FastAPI | Interactive manual API testing at `/docs` | Teammates and reviewers can inspect request fields and try endpoints without writing a separate client |
| Postman | Optional external manual-testing client | Makes authentication settings and repeated API requests explicit; it is not an application dependency |

Tests use a separate disposable PostgreSQL database, not the shared development data. [smoke.py](../backend/app/smoke.py) supplements them by calling the running API and waiting for the independent worker. See the [manual test guide](MANUAL_TESTING.md) for manual steps and result records.

## Languages and file formats

| Language or format | Use |
| --- | --- |
| Python | Application logic, tests, configuration initialization and migration functions |
| SQL | Table creation, constraints and database queries; many queries are generated by SQLAlchemy |
| YAML | Compose service definitions and GitHub Actions workflows |
| TOML | pytest and Ruff configuration in `pyproject.toml` |
| Markdown | Reviewer and developer documentation |
| Mako | Alembic's revision-file template, not a frontend rendering layer |

Runtime messages and reviewer documentation are English. Source comments are bilingual; Chinese prose guides live under `docs/developer/zh-CN/`. Valid Unicode user input is preserved.

## Version maintenance and future work

The package versions above come from the committed [runtime requirements](../backend/requirements.txt) and [development requirements](../backend/requirements-dev.txt). They describe this repository, not a recommendation to use the newest release. Update the `.in` lists and regenerate the `.txt` files together, then rebuild and test before committing a dependency change.

Supporting transitive packages are also locked in those files. Installing a package as a dependency does not mean every feature it offers is configured; for example, the presence of an OpenTelemetry dependency is not an implemented tracing system.

React, model APIs, approval workflows, SSE, simulated business services, production Swarm deployment, provider monitoring and automated backups remain planned work. There is no current Redis, Celery or separate message broker service. See the [architecture handover](AGENTHUB_TEAM_HANDOVER.md) for the broader proposal.

Official references: [FastAPI features](https://fastapi.tiangolo.com/features/), [SQLAlchemy sessions](https://docs.sqlalchemy.org/en/20/orm/session_basics.html), and [Docker Compose application model](https://docs.docker.com/compose/intro/compose-application-model/).
