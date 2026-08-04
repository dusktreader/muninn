# Muninn API

The `api/` subproject contains Muninn's FastAPI service and its persistence, storage, queue, processing,
and test code. It is an independently packaged Python project managed with `uv`.


## Source layout

- `muninn/api/`: FastAPI application, health and conversation
  routes, API schemas, and API-specific errors and constants.
- `muninn/auth/`: Keycloak OIDC bearer-token validation and capability checks.
- `muninn/db/`: SQLAlchemy models, sessions, and database-backed idempotency records.
- `muninn/storage/`: MinIO/S3 object storage for recordings and immutable processed artifacts.
- `muninn/queue/`: NATS JetStream subjects, message contracts, queue management, and queue object storage.
- `muninn/job_manager/`: Outbox dispatch and worker-completion handling.
- `muninn/worker/`: NATS job consumption, AssemblyAI transcription, and LLM Gateway summarization.
- `muninn/mcp/`: Bounded MCP context and agent-note route implementation and limits. The application
  currently mounts the health and conversation routers in `muninn/api/main.py`.
- `muninn/schemas.py`, `muninn/constants.py`, `muninn/utils.py`, and related top-level
  modules: shared schemas, enums, configuration, logging, exceptions, and utilities.
- `migrations/`: Alembic configuration and database revisions.
- `tests/`: unit, integration, provider, end-to-end, API, auth, and traceability tests.

PostgreSQL stores metadata and object keys. MinIO stores conversation content. NATS carries processing jobs and
completion events; the job manager and worker processes are exposed as `muninn-job-manager` and `muninn-worker`.


## Local configuration

Use Python 3.13 and `uv`. PostgreSQL is required for the API. Configure
these `MUNINN_` settings as needed for the local services:

- `MUNINN_DATABASE_URL` for PostgreSQL.
- `MUNINN_OBJECT_STORE_ENDPOINT`, `MUNINN_OBJECT_STORE_BUCKET`, and optional access and secret keys for MinIO.
- `MUNINN_NATS_URL` for NATS JetStream, used by the job manager and worker.
- `MUNINN_OIDC_ISSUER_URL`, `MUNINN_OIDC_JWKS_URL`, and optionally `MUNINN_OIDC_AUDIENCE` for Keycloak authentication.
- `MUNINN_ASSEMBLYAI_API_KEY`, `MUNINN_ASSEMBLYAI_TRANSCRIPTION_MODEL`, and
  `MUNINN_ASSEMBLYAI_SUMMARY_MODEL` when running processing workers.
- `MUNINN_ASSEMBLYAI_API_BASE_URL` and `MUNINN_ASSEMBLYAI_LLM_BASE_URL` when using non-default endpoints.

The API authenticates bearer tokens against Keycloak and authorizes operations by capability. Mutation endpoints use
caller-scoped idempotency keys and request fingerprints so safe retries can replay completed operations.


## Commands

From the repository root, prefix targets with `make -C api`. From this directory, run the same targets with `make`:

| Target        | Purpose                               |
| ------------- | ------------------------------------- |
| `help`        | Show available targets                |
| `qa`          | Run tests, type checks, and linters   |
| `test`        | Run unit and integration-marked tests |
| `integration` | Run integration tests only            |
| `types`       | Run `ty` static type checks           |
| `lint`        | Run Ruff and Typos                    |
| `serve`       | Run the local API on port 5000        |
| `migrate`     | Apply pending Alembic migrations      |

```shell
# From the repository root
uv sync --directory api
make -C api help
make -C api qa
make -C api serve

# From api/
make test
make integration
make types
make lint
make migrate
```
