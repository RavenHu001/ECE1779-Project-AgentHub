# AgentHub Backend

AgentHub is a University of Toronto ECE1779 team project. This repository implements authenticated conversations, durable message submission, PostgreSQL-backed jobs, and a separate worker with optional OpenAI text chat. The default fixed-reply mode requires no API key and makes no paid model requests.

For this milestone's worker setup, model configuration, limits and verification, see [Worker baseline](docs/WORKER_BASELINE.md). A lightweight browser test client is available at `/`; the API documentation remains at `/docs`.

**Start here to run or review the implemented backend.** See the [database reference](docs/BACKEND_DATABASE.md) for field definitions and the [documentation index](docs/README.md) for architecture and planning material. Chinese-language notes are kept under [developer documentation](docs/developer/zh-CN/README.md).

For a short explanation of the source files and how they work together, read the [code structure guide](docs/CODE_STRUCTURE.md).

The [technology stack guide](docs/TECH_STACK.md) explains the current tools, versions and why they fit this implementation.

## Current scope

The API and worker share a Python 3.13 image. PostgreSQL 17 stores users, conversations, messages and jobs on a persistent volume. Startup waits for database readiness, applies Alembic migrations, initializes the first account, then starts the API and worker.

By default the worker responds with `Message received.`. Set `REPLY_MODE=openai` and configure a private API key to enable real model replies with bounded context, output and retries. It does not perform business operations. Human approvals, SSE, simulated refunds, backups and production Swarm deployment remain planned work. The included GitHub Actions workflow builds and tests the containers; it does not publish images or deploy to a remote server.

## Quick start

Requirements: Git, a running Docker Engine, and Docker Compose v2 or newer with `--wait` support. The configuration initializer uses only the Python standard library and needs Python 3.10 or newer on the host. Use `python3` instead of `python` on Linux if necessary.

Run these commands on your development machine or the team's shared Linux server:

```bash
git clone https://github.com/RavenHu001/ECE1779-Project-AgentHub.git
cd ECE1779-Project-AgentHub
python scripts/init_env.py
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps -a
```

If you already have a checkout, start from its root directory rather than cloning it again.

The initializer creates an ignored `.env` with independent random database and bootstrap-account passwords. It never overwrites an existing file. Open `.env` privately to read `BOOTSTRAP_USERNAME` and `BOOTSTRAP_PASSWORD`; do not include credentials in commits, screenshots or review comments.

If host Python is unavailable, copy `.env.example` to `.env` and replace both password placeholders with independent random passwords before starting the containers.

After startup:

- Health check: <http://127.0.0.1:8000/health>.
- Browser test client: <http://127.0.0.1:8000/>.
- Interactive API documentation: <http://127.0.0.1:8000/docs>.
- `migrate` and `bootstrap` should exit with code 0; these are one-shot services.
- `api` should be healthy and `worker` should remain running. PostgreSQL has no published host port.

## Team workflow and shared access

Develop on separate branches and submit pull requests. CI builds the images, runs lint and PostgreSQL integration tests, then exercises the live API and independent worker. After review and merge, one maintainer updates the shared environment:

```bash
git pull --ff-only
docker compose up -d --build --wait
docker compose exec -T api python -m app.smoke
```

Run this update from the agreed integration branch, with a clean shared checkout. Do not switch branches or modify the shared database concurrently. Members can also use independent checkouts for isolated development.

By default, the API binds to the server's loopback address. To reach a remote team's development server, keep this SSH tunnel open on your own machine:

```bash
ssh -N -L 18000:127.0.0.1:8000 your-user@your-server
```

Then open <http://127.0.0.1:18000/docs> or use `http://127.0.0.1:18000` as the Postman base URL. The maintainer supplies the actual server address and SSH access.

Development authentication uses HTTP Basic with salted scrypt password hashes. Use an SSH tunnel or HTTPS. A shared HTTPS endpoint requires a separately configured reverse proxy and certificate; changing `API_BIND_HOST` alone does not provide TLS. The final login/session mechanism can be refined later.

### Individual accounts

Create a separate account for each team member:

```bash
docker compose run --rm admin create-user alice
docker compose run --rm admin create-user bob
```

The command asks for and confirms the password without placing it in shell history. Usernames contain 1–64 ASCII letters, digits, dots, hyphens or underscores and begin with a letter or digit; they are normalized to lowercase. HTTP Basic passwords must contain at least 12 ASCII characters. Each account can access only its own conversations, messages and jobs.

To change an existing password:

```bash
docker compose run --rm admin set-password alice
```

Editing `.env` does not reset an existing account. Bootstrap deliberately preserves stored passwords. If you change the bootstrap account's password, also update its `.env` credential and recreate the relevant services before using `app.smoke`.

### Independent environments on one host

Use separate checkout directories, project names and API ports. For example, in a new checkout:

```bash
python scripts/init_env.py --project-name agenthub-alice --api-port 8001
docker compose up -d --build --wait
```

`COMPOSE_PROJECT_NAME` separates containers, networks, volumes and image names; the API port avoids host-port collisions. Do not reuse a project name across independent environments. Each environment reaches its own database at `db:5432` inside its Compose network.

## Try the API

In Swagger UI, click **Authorize** and enter your assigned credentials. In Postman, select **Authorization → Basic Auth**. The business endpoints require authentication; `/health` does not.

1. Call `POST /conversations` with the following JSON. Save the returned `id` as `conversation_id`.

   ```json
   {"title": "My first backend test"}
   ```

   The response is `201 Created`. Sending `{}` uses the title `New conversation`.

2. Call `POST /conversations/{conversation_id}/messages`:

   ```json
   {
     "content": "Hello",
     "submission_key": "cbd2fe4d-3781-4856-b416-44242a730dac"
   }
   ```

   The `202 Accepted` response contains `message_id` and `job_id`. Reuse the same key only when retrying the same submission. A genuinely new message needs a new UUID. Reusing a key with different text returns `409 Conflict`.

3. Call `GET /conversations/{conversation_id}/messages`. The worker checks for work every second by default. You should see `Hello` and `Message received.`; the reply's `reply_to_message_id` points to the user message.

4. Call `GET /jobs/{job_id}` to distinguish `QUEUED`, `RUNNING`, `SUCCEEDED` and `FAILED` work.

Generate a UUID with `[guid]::NewGuid()` in PowerShell or `python -c "import uuid; print(uuid.uuid4())"` where Python is available.

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Check database connectivity and report fixed-reply mode |
| `GET /me` | Return the authenticated account without password fields |
| `POST /conversations` | Create a conversation owned by the current account |
| `GET /conversations` | List your conversations, most recently updated first |
| `POST /conversations/{id}/messages` | Atomically save a message and its job; return stable IDs on retry |
| `GET /conversations/{id}/messages` | Read messages in sequence order using `after_seq` and `limit` |
| `GET /jobs/{id}` | Read the processing state of your message's job |

Both list endpoints default to 100 records and accept `limit` from 1 to 500. Requests for another account's resource return `404`; missing or invalid credentials return `401`; invalid input returns `422`; database unavailability returns `503`.

System-generated defaults, validation errors, CLI prompts, logs and API descriptions are English. Source comments are bilingual. User-provided text remains Unicode and is stored as submitted.

## Verify the live system and persistence

For step-by-step Swagger or Postman checks and a result record template, use the [manual testing guide](docs/MANUAL_TESTING.md).

Run the included smoke check without copying credentials onto the command line:

```bash
docker compose exec -T api python -m app.smoke
```

It creates a conversation, submits a message twice using the same key, waits for the separate worker, verifies a single reply and prints the conversation UUID.

To verify persistence, record that UUID, recreate the containers without deleting volumes, and substitute the UUID below:

```bash
docker compose down
docker compose up -d --wait
docker compose exec -T api python -m app.smoke --verify-conversation YOUR_CONVERSATION_UUID
```

Ordinary `down` retains the database volume. **Do not use `down --volumes` or `down -v` on a shared environment: those commands delete its database volume.** Persistent volumes are not backups.

## Develop and test

Code is copied into images rather than bind-mounted. Rebuild after editing code:

```bash
docker compose up -d --build --wait
docker compose logs --tail 100 api worker
```

Build and run the test image:

```bash
docker compose --profile test build test
docker compose run --rm --no-deps test sh -c "ruff check . && ruff format --check ."
docker compose run --rm test
```

Tests use a separate `test-db` container and the disposable `agenthub_test` database, never the development `db`. Fixtures refuse database names without the `_test` suffix. The test database uses a temporary memory-backed mount and loses its data when stopped.

Integration tests cover message submission, model-provider contracts using stubbed HTTP, fixed replies, ordered conversation context, bounded retries, duplicate requests, ownership, concurrent claims, lease renewal, expired-claim recovery, stale-worker fencing and migrations. Defaults are a 60-second renewable lease and at most three claims. Transient model failures requeue with a delay; non-retryable processing exceptions are recorded as failures.

Stop only the temporary test database, or start a second worker, with:

```bash
docker compose stop test-db
docker compose up -d --scale worker=2 --wait
```

## Coordinate schema and dependency changes

Alembic migrations are committed with application code. The one-shot `migrate` service runs before account initialization; individual API and worker replicas do not run migrations themselves. Serialize updates to the shared environment.

For a schema change:

1. Update `backend/app/models.py` on your branch.
2. Add a new migration rather than changing a revision that has already been applied.
3. Review the SQL and test upgrades in an isolated environment.
4. Reconcile migration branches with other contributors before updating the shared database.

Inspect migration state:

```bash
docker compose run --rm migrate alembic current
docker compose run --rm migrate alembic heads
```

To generate a revision into your independent checkout, temporarily mount the source directory:

```bash
docker compose run --rm --user 0 --volume "${PWD}/backend:/app" migrate alembic revision --autogenerate -m add_example
```

On Linux, generated files may need their ownership adjusted after this root invocation. Review generated operations, particularly data conversions and drops, then rebuild. Do not repair a populated shared database by deleting volumes or downgrading it. Rolling back an image does not roll back its schema.

The original `0001_initial` revision is preserved. `0002_english_defaults` changes the default for future conversation titles to English. Existing titles and messages are not rewritten.

Dependency ranges live in `.in` files; resolved versions live in `.txt` files. Maintainers can regenerate the runtime lock first, then the development lock, with uv:

```bash
uv pip compile backend/requirements.in --python-version 3.13 --python-platform x86_64-unknown-linux-gnu -o backend/requirements.txt
uv pip compile backend/requirements-dev.in --python-version 3.13 --python-platform x86_64-unknown-linux-gnu -o backend/requirements-dev.txt
```

Rebuild, test and review both dependency changes together.

## Repository layout

```text
.github/workflows/ci.yml   Container builds, lint and integration tests
.env.example              Shareable configuration template
compose.yaml              Runtime, migrations, tools and isolated test services
scripts/init_env.py        Ignored configuration and random-credential generator
backend/app/               API, authentication, models, worker and admin commands
backend/migrations/        Versioned database migrations
backend/tests/             PostgreSQL integration tests
docs/                      English reviewer documentation and architecture notes
docs/developer/zh-CN/      Chinese team guides and earlier planning drafts
```
