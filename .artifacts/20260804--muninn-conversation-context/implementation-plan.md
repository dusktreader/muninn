# Implementation Plan: Muninn first release

This plan turns the approved Muninn design into an executable release sequence. It preserves one authenticated service
authority while adding Android and web capture, deferred Android synchronization, server processing,
CLI/TUI-ready client capabilities, MCP context access, and a reproducible homelab deployment.


## Goal

Deliver an English-only first release for one self-hosted homelab deployment. Android and web both capture audio.
Android alone supports offline capture, encrypted local audio cache, and deferred synchronization. The web client
requires connectivity for upload, fetches audio only on demand, and never caches audio locally.

The API is authoritative after acknowledgement. PostgreSQL 18 stores metadata and lifecycle state, MinIO stores
immutable artifacts, and NATS JetStream provides durable work delivery. Zen API performs transcription and summarization
through a server-side adapter. Human and agent notes are timestamped. MCP exposes bounded reads and `add-agent-note`
only. Device authorization authenticates CLI/TUI clients. Helm targets homelab Kubernetes, DevSpace targets local
development, and Cloudflare Tunnel is supplied as deployment configuration. Deleting a conversation deletes
the whole conversation; the first release has no archive feature or archive state.


## Project Commands

The root Makefile is the canonical execution interface, with component targets dispatched to `api/`, `web-ui/`,
`mobile-ui/`, and `infra/`. Task 01 must implement the first-release gates while retaining the established component
ownership. The graph is a dependency reference; every target has its own executable procedure below.

| Target                        | Dependencies                                                    | Recipe or required evidence                                                                                                                                                                                                                                                                               |
| ----------------------------- | --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `qa/test`                     | none                                                            | `uv run --project api pytest api/tests -m "not live and not deployed"`; emits `.coverage.xml` and passes service/container tests                                                                                                                                                                          |
| `qa/types`                    | none                                                            | `uv run --project api ty check api/src/muninn api/tests`                                                                                                                                                                                                                                                  |
| `qa/lint`                     | none                                                            | `uv run --project api ruff check api/src/muninn api/tests` and `uv run --project api typos api/src/muninn api/tests`                                                                                                                                                                                      |
| `qa/markdown`                 | none                                                            | `node ~/.agents/tools/check-markdown-format.mjs .`                                                                                                                                                                                                                                                        |
| `qa/provider`                 | `qa/test`                                                       | Explicit opt-in command. Requires `MUNINN_ZEN_BASE_URL`, `MUNINN_ZEN_API_KEY`, `MUNINN_ZEN_TRANSCRIPTION_MODEL`, and `MUNINN_ZEN_SUMMARY_MODEL`; runs `uv run --project api pytest api/tests/provider -m live` and fails closed when credentials are missing. Normal `qa` excludes `live` and `deployed`. |
| `qa/full`                     | `qa/test qa/types qa/lint qa/markdown client/test client/build` | Runs all non-deployment gates and prints `All quality checks pass!`                                                                                                                                                                                                                                       |
| `qa`                          | `qa/full qa/traceability`                                       | Runs the full non-live, non-deployed release gate through `qa/test`; it does not invoke `qa/provider` or `qa/deploy`                                                                                                                                                                                      |
| `qa/traceability`             | none                                                            | `uv run --project api python api/tests/traceability/check_matrix.py docs/acceptance-traceability.md`                                                                                                                                                                                                      |
| `qa/deploy`                   | `deploy/lint`                                                   | Explicit opt-in command. Installs the homelab plus QA fake-provider values, runs `uv run --project api pytest api/tests/deploy/test_release.py -m deployed`, and collects `.artifacts/deploy/` evidence                                                                                                   |
| `client/web-install`          | none                                                            | `corepack pnpm --dir web-ui install --frozen-lockfile`                                                                                                                                                                                                                                                    |
| `client/web-check`            | `client/web-install`                                            | `pnpm --dir web-ui typecheck`, `pnpm --dir web-ui lint`, and `pnpm --dir web-ui build`                                                                                                                                                                                                                    |
| `client/build`                | `client/web-check`                                              | `pnpm --dir web-ui build` and `mobile-ui/gradlew --no-daemon -p mobile-ui assembleDebug`                                                                                                                                                                                                                  |
| `client/android-instrumented` | none                                                            | Runs package `app.muninn.capture` on a connected API 35 emulator or device                                                                                                                                                                                                                                |
| `client/test`                 | `client/web-install client/android-instrumented`                | Runs web, browser, Android unit, lint, and instrumented tests                                                                                                                                                                                                                                             |
| `client/cli`                  | `qa/test`                                                       | `uv run --project api pytest api/tests/client/test_cli.py api/tests/client/test_auth.py`                                                                                                                                                                                                                  |
| `api/serve`                   | none                                                            | `make -C api serve`                                                                                                                                                                                                                                                                                       |
| `dev/up`                      | `web-ui/build`                                                  | Builds the web bundle (installing locked dependencies first), starts the Vite server, then invokes DevSpace with force-build and force-deploy for the local API image and service stack; it does not run the production Helm deploy                                                                       |
| `deploy/lint`                 | none                                                            | Lints once, then templates homelab production values and homelab plus QA fake-provider values under `infra/helm/muninn/`                                                                                                                                                                                  |
| `deploy/install`              | `deploy/lint`                                                   | `helm upgrade --install muninn infra/helm/muninn --namespace muninn --create-namespace --values infra/helm/muninn/values-homelab.yaml`                                                                                                                                                                    |
| `backup/create`               | `deploy/install`                                                | `infra/backup/create.sh`; writes a fenced recovery bundle and prints `RECOVERY_POINT_ID`                                                                                                                                                                                                                  |
| `backup/restore`              | none                                                            | `infra/backup/restore.sh $(RECOVERY_POINT_ID)`; requires workers stopped and operator confirmation                                                                                                                                                                                                        |
| `backup/quarantine`           | none                                                            | `infra/backup/quarantine.sh $(RECOVERY_POINT_ID)` isolates objects outside the recovery manifest                                                                                                                                                                                                          |
| `backup/verify`               | `backup/quarantine`                                             | `infra/backup/verify-restore.sh $(RECOVERY_POINT_ID)` verifies the restored recovery point                                                                                                                                                                                                                |
| `backup/enable-workers`       | `backup/verify`                                                 | `infra/backup/enable-workers.sh $(RECOVERY_POINT_ID)` rebuilds NATS JetStream and enables workloads                                                                                                                                                                                                       |


### Install Python dependencies

Prerequisites:

- Python 3.13.5, managed by `uv` 0.8.4
- Repository checkout at the worktree root

Command:

```shell
uv sync --project api --locked
```

Expected Output:

`api/uv.lock` is unchanged, the API virtual environment is synchronized,
and `uv run --project api python -c "import muninn"` exits successfully.


### `client/web-install`

Prerequisites:

- Node.js 22.14.0 LTS
- `corepack` with pnpm 10.6.2 enabled

Command:

```shell
make client/web-install
```

Expected Output:

`web-ui/pnpm-lock.yaml` is honored exactly and the web workspace installs without modifying the lockfile.


### `client/build`

Prerequisites:

- Java 17.0.13
- Android SDK platform 35 and build-tools 35.0.0
- Android Gradle Plugin 8.7.3 through the checked-in Gradle wrapper 8.9
- Node.js and pnpm versions above

Command:

```shell
make client/build
```

Expected Output:

The web production bundle and Android debug APK build successfully from locked dependencies.


### `api/serve`

Prerequisites:

- `make dev/up` for PostgreSQL 18, MinIO, and NATS JetStream, or a configured integration environment

Command:

```shell
make api/serve
```

Expected Output:

Uvicorn serves `http://localhost:5000`. `/healthz` reports process health only, and
`/readyz` reports dependency readiness without returning conversation content.


### `qa/test`

Prerequisites:

- Python dependencies and a container runtime for the pinned service fixtures

Command:

```shell
make qa/test
```

Expected Output:

Unit, API, PostgreSQL/MinIO/NATS JetStream, Keycloak, worker, MCP, contract, and recovery tests pass. Pytest excludes
every `live` and `deployed` test. Zen calls use deterministic provider fakes unless
`make qa/provider` is explicitly requested with deployment credentials.


### `qa`

Prerequisites:

- All Python, web, Java, Android SDK, emulator, and container-runtime prerequisites of dependent targets

Command:

```shell
make qa
```

Expected Output:

Python tests excluding the `live` and `deployed` markers, Ruff, Ty, Typos, web typecheck/lint/test/build, Android
lint/test/build, Markdown validation, and the acceptance traceability check pass with zero errors.


### `dev/up`

Prerequisites:

- DevSpace 6.3.3
- microk8s 1.31.4 or an equivalent local Kubernetes 1.31 cluster
- Node.js 22 and Corepack for the pinned pnpm web toolchain

Command:

```shell
make dev/up
```

Procedure:

1. Run `make -C web-ui install` to install locked web dependencies.
2. Run `make -C web-ui build` to build the Vite bundle.
3. Start `make -C web-ui serve` in the background on port 4173; keep it running while DevSpace starts.
4. Run `devspace dev --force-build --force-deploy --tag local-$(date +%s)
   --config devspace.yaml --profile local --namespace muninn-local`.

Expected Output:

DevSpace force-builds the API service image and force-deploys the local API and service stack with API port 5000,
PostgreSQL 18, MinIO, and NATS JetStream. The local profile uses local authentication and a fake provider; it does not
require production Keycloak credentials or change production identity, encryption, artifact grants, or deletion
behavior. `dev/up` is local development only and does not run the production Helm deployment.


### `deploy/lint`

Prerequisites:

- Helm 3.16.4
- Kubernetes 1.31 API compatibility
- Checked-in chart dependencies and `values-homelab.yaml`

Command:

```shell
make deploy/lint
```

Expected Output:

Helm lint and template rendering pass. `api/tests/deploy/test_topology.py` confirms that homelab values enable only
embedded PostgreSQL, NATS JetStream, and MinIO, require external Keycloak, and reject every external store endpoint key.


### `qa/deploy`

Prerequisites:

- A clean Kubernetes or microk8s 1.31.4 context selected through `KUBECONFIG`
- Helm 3.16.4 and an existing Cloudflare credential Secret
- Checked-in `infra/helm/muninn/values-qa-fake-provider.yaml` with non-production fake-provider settings

Command:

```shell
make qa/deploy
```

Expected Output:

The target installs with this exact values composition before running the deployed test:

```shell
helm upgrade --install muninn infra/helm/muninn --namespace muninn --create-namespace \
  --values infra/helm/muninn/values-homelab.yaml \
  --values infra/helm/muninn/values-qa-fake-provider.yaml
uv run --project api pytest api/tests/deploy/test_release.py -m deployed \
  --junitxml=.artifacts/deploy-junit.xml
```

The fake adapter handles every provider call. The test authenticates through Keycloak, captures and commits a recording,
completes fake transcription and summarization, deletes the conversation, and verifies that no stale client or worker
request resurrects it. The test passes and writes manifests, pod logs,
JUnit output, and flow evidence under `.artifacts/deploy/`.


### `client/cli`

Prerequisites:

- Python dependencies and the Keycloak test fixture

Command:

```shell
make client/cli
```

Expected Output:

The CLI completes Keycloak device authorization, stores refresh credentials only in the OS keyring, lists and inspects
owned conversations, edits metadata with revision checks, reports lifecycle
state, and requires explicit confirmation for whole-conversation deletion.


### `qa/types`

Prerequisites:

- Python dependencies installed with `uv sync --project api --locked`

Command:

```shell
make qa/types
```

Expected Output:

`uv run --project api ty check api/src/muninn api/tests` exits with no type errors.


### `qa/lint`

Prerequisites:

- Python dependencies installed with `uv sync --project api --locked`

Command:

```shell
make qa/lint
```

Expected Output:

Ruff and Typos report no errors in `api/src/muninn` or `api/tests`.


### `qa/markdown`

Prerequisites:

- Node.js 22.14.0 and `~/.agents/tools/check-markdown-format.mjs`

Command:

```shell
make qa/markdown
```

Expected Output:

The repository Markdown validator reports no formatting errors.


### `qa/provider`

Prerequisites:

- Python dependencies
- `MUNINN_ZEN_BASE_URL`, `MUNINN_ZEN_API_KEY`, `MUNINN_ZEN_TRANSCRIPTION_MODEL`,
  and `MUNINN_ZEN_SUMMARY_MODEL` set for a non-production Zen account

Command:

```shell
make qa/provider
```

Expected Output:

`uv run --project api pytest api/tests/provider -m live` passes as an explicit opt-in suite. If any required
variable is absent, the target exits before pytest and names every missing variable.


### `qa/full`

Prerequisites:

- Every prerequisite listed for its dependent targets, including one connected API 35 emulator or device

Command:

```shell
make qa/full
```

Expected Output:

All non-deployment Python and client gates pass, including Android
instrumentation, and the final line is `All quality checks pass!`.


### `qa/traceability`

Prerequisites:

- Python dependencies and a current `docs/acceptance-traceability.md`

Command:

```shell
make qa/traceability
```

Expected Output:

`api/tests/traceability/check_matrix.py` reports one complete evidence row for each `D-AC01` through `D-AC30`.


### `client/web-check`

Prerequisites:

- Node.js 22.14.0, pnpm 10.6.2, and a successful `make client/web-install`

Command:

```shell
make client/web-check
```

Expected Output:

Web type checking, linting, and the production build pass without modifying `web-ui/pnpm-lock.yaml`.


### `client/android-instrumented`

Prerequisites:

- Java 17.0.13, Android SDK platform 35, and build-tools 35.0.0
- One clean API 35 emulator or physical device that reports `device` through `adb devices`
- Controllable microphone, notification, foreground-service, and audio-output test permissions

Command:

```shell
make client/android-instrumented
```

Expected Output:

This exact package command reports zero failed instrumentation tests:

```shell
mobile-ui/gradlew --no-daemon -p mobile-ui connectedDebugAndroidTest \
  -Pandroid.testInstrumentationRunnerArguments.package=app.muninn.capture
```


### `client/test`

Prerequisites:

- Web dependencies and the `client/android-instrumented` emulator or device prerequisites

Command:

```shell
make client/test
```

Expected Output:

Vitest, Playwright, Android unit tests, Android lint, and `client/android-instrumented`
pass without production store credentials or live-provider calls.


### `deploy/install`

Prerequisites:

- Helm 3.16.4, a clean Kubernetes 1.31.4 context, and successful `make deploy/lint`
- Operator-supplied `global.publicHostname`, external Keycloak issuer Secret, and Cloudflare Tunnel Secret
- Production Zen model IDs, quota profile, base URL, and API-key Secret

Command:

```shell
make deploy/install
```

Expected Output:

The target runs this exact command:

```shell
helm upgrade --install muninn infra/helm/muninn --namespace muninn --create-namespace \
  --values infra/helm/muninn/values-homelab.yaml
```

Readiness succeeds. Only web, API, and MCP ingress is public; PostgreSQL, MinIO, NATS
JetStream, Keycloak administration, and operational endpoints remain private.


### `backup/create`

Prerequisites:

- A healthy homelab release, restic 0.17.3, and the backup repository Secret
- No Helm upgrade or Keycloak realm change in progress

Command:

```shell
make backup/create
```

Expected Output:

One coherent fenced bundle is stored and the command prints exactly one `RECOVERY_POINT_ID=<uuid>`
line. Concurrent mutating API calls receive the documented retryable backup response.


### `backup/restore`

Prerequisites:

- A verified bundle identified by `RECOVERY_POINT_ID` and operator confirmation of the destructive restore

Command:

```shell
make backup/restore RECOVERY_POINT_ID=<uuid>
```

Expected Output:

API and worker replicas remain zero. PostgreSQL, exact MinIO versions and delete markers, Keycloak
realm state, and Helm state are restored without dispatching NATS JetStream work.


### `backup/quarantine`

Prerequisites:

- Restore completed for the same recovery point with workers disabled

Command:

```shell
make backup/quarantine RECOVERY_POINT_ID=<uuid>
```

Expected Output:

Objects absent from `manifest-snapshot.json` move to `quarantine/<uuid>/`. Exact version IDs, delete-marker IDs,
and the object count are written to `.artifacts/recovery/<uuid>/quarantine.json`.


### `backup/verify`

Prerequisites:

- Restore and quarantine completed for the same recovery point

Command:

```shell
make backup/verify RECOVERY_POINT_ID=<uuid>
```

Expected Output:

Fence, PostgreSQL snapshot, manifest digest, MinIO inventory, tombstone precedence, quarantine
inventory, and absence of tombstoned jobs verify. Workers remain disabled on any mismatch.


### `backup/enable-workers`

Prerequisites:

- Successful `make backup/verify RECOVERY_POINT_ID=<uuid>`

Command:

```shell
make backup/enable-workers RECOVERY_POINT_ID=<uuid>
```

Expected Output:

NATS JetStream rebuild from PostgreSQL. API and worker deployments return to their
recorded replica counts, and readiness passes without dispatching tombstoned work.


----

## Project Standards

- [Approved design plan](design-plan.md)
- [Project README](../../README.md)
- [API project configuration](../../api/pyproject.toml)
- [API lockfile](../../api/uv.lock)
- [Project Makefile](../../Makefile)
- [Python package](../../api/src/muninn)
- [API tests](../../api/tests)
- [Web client](../../web-ui)
- [Android client](../../mobile-ui)
- [Infrastructure](../../infra)

All links resolve within the current worktree from this artifact directory. Update the README to remove the statements
that recording deletion preserves derived artifacts and that archive commands exist.
The implemented policy is whole-conversation deletion with no archival.


## Relevant Skills

- `execute-implementation-plan`
- `execute-implementation-task`
- `review-implementation-execution`
- `review-code`


## Execution

### 01: Establish release contracts, toolchains, and traceability

Create the repository surfaces and contracts before feature work. Do not add a second persistence authority.


#### Acceptance Criteria

- AC01: `docs/release-contract.md` names Android and web as capture clients, Android-only offline/cache/deferred-sync
  behavior, web no-cache behavior, CLI device authorization, MCP `add-agent-note` only, and English Zen processing,
  `Speaker <N>` labels with optional human names, Helm, DevSpace, Cloudflare Tunnel
  deployment configuration, whole- conversation deletion, and no archival.
- AC02: `docs/acceptance-traceability.md` contains exactly one row for every design criterion `D-AC01` through `D-AC30`,
  with owning task, exact automated command or test path, and named manual evidence where applicable.
- AC03: The API/domain, clients, workers, and MCP adapter have one documented boundary; only the
  API/domain writes PostgreSQL and only service code accesses MinIO or NATS JetStream.
- AC04: The repository contains the exact dependency/build files
  named in Technical Notes and every lockfile is checked in.
- AC05: `api/pyproject.toml` registers the `live` and `deployed` pytest markers. `qa/test`, `qa/full`, and `qa` exclude
  both; only `qa/provider` and `qa/deploy` explicitly select `live` and `deployed`, respectively.


#### Steps

- Add `docs/release-contract.md`, `docs/acceptance-traceability.md`, and `docs/api-contract.md` before implementation.
- Add the Python dependencies and development tools to `api/pyproject.toml`, update `api/uv.lock` with `uv 0.8.4`, and
  add `api/alembic.ini`, `api/migrations/`, and service settings without changing the Python requirement from 3.13.5.
- Add the Android Gradle files and wrapper named in Technical Notes, plus the manifest and source/test sets.
- Add the web package, lockfile, TypeScript, Vite, and browser-test files named in Technical Notes.
- Replace the scaffold recipes with the exact target graph in Project Commands. Add `qa/markdown`, `qa/provider`,
  `qa/deploy`, `client/web-install`, `client/web-check`, `client/build`, `client/android-instrumented`, `client/test`,
  `client/cli`, all five `backup/*` targets, `deploy/lint`, `deploy/install`, `dev/up`, and `qa/traceability`. Make
  `qa/full` include `client/android-instrumented` through `client/test`. Register `live` and `deployed` under
  `[tool.pytest.ini_options]` in `api/pyproject.toml`; run normal Python QA with
  `uv run --project api pytest api/tests -m "not live and not deployed"`.
- Add `api/tests/commands/test_make_targets.py`, `api/tests/test_dependency_inventory.py`,
  `api/tests/traceability/check_matrix.py`, `api/tests/deploy/test_release.py`, `api/tests/provider/`, and the
  documented artifact collection paths. Run `uv run --project api pytest api/tests/commands/test_make_targets.py
  api/tests/test_dependency_inventory.py`, verify every target with `make -n`, and run `make qa/markdown` before Task 01
  closes. Make the command-level test assert that `qa/test`, `qa/full`, and `qa` cannot select `live` or `deployed`,
  while `qa/provider` and `qa/deploy` select their respective markers only through explicit invocation.
- Make `docs/api-contract.md` concrete at creation: add the authentication matrix, `global.publicHostname` redirect
  derivation, stable auth error table, lifecycle tables, upload contracts, and note contract reserved for Task 09.
- Write the initial traceability rows as contracts are established.
  Later tasks update evidence paths, not the design AC identifiers.


### 02: Define persistence, lifecycle state, and deletion behavior

Replace the scaffold's single status and nullable object keys with PostgreSQL-first entities and
independently testable lifecycle dimensions. SQLite remains disposable development data only.


#### Acceptance Criteria

- AC01: A clean PostgreSQL 18 database reaches the current schema through Alembic migrations; the migration policy is
  forward-only and downgrade safety is tested by restoring a pre-migration backup.
- AC02: The schema stores capture time, owner subject, revisions, operations, upload sessions, artifact versions,
  processing attempts, speaker labels, audit events, tombstones, and deletion completion state.
- AC03: Legal transitions and retry results match the lifecycle tables
  in Technical Notes. No archive or archived state exists.
- AC04: Capture time is immutable after acceptance. Metadata patches cannot change lifecycle
  state. Stale updates return current values, and tombstones prevent resurrection.
- AC05: Constraints prevent duplicate artifacts or operations and references to deleted conversations.


#### Steps

- Split `api/src/muninn/models.py` into domain modules while retaining typed SQLAlchemy metadata.
- Add migrations for conversations, revisions, operations, upload sessions, artifact manifests, processing, speakers,
  audit events, tombstones, and deletion jobs. Task 09 solely owns note tables and migrations.
- Add repositories that enforce owner filtering, revision preconditions, tombstone precedence, and transactional replay.
- Add unit and PostgreSQL integration tests for every table transition and every deletion outcome.
- Add the lifecycle and deletion tables to `docs/api-contract.md` and link each state test from the traceability matrix.


### 03: Implement Keycloak authentication and authorization contracts

Implement issuer discovery, JWT validation, public-client PKCE, CLI device authorization, and delegated MCP sessions.
Authorization is capability-based and enforced on the API, not inferred from UI controls.


#### Acceptance Criteria

- AC01: Invalid, expired, revoked, not-yet-valid, wrong-issuer, wrong-audience, and insufficient-capability credentials
  return the exact stable codes and HTTP statuses in Technical Notes without owner data.
- AC02: Android and web use authorization code plus PKCE with no client secret, exact registered
  redirect URIs, and issuer/audience values from the authentication matrix.
- AC03: CLI/TUI use device authorization grant and store refresh tokens in the OS
  keyring, never in plaintext config, logs, command arguments, or the repository.
- AC04: MCP sessions map a user subject and delegated agent session to a short-lived, revocable
  capability set. The only write capability in the first release is `notes:agent:create`.
- AC05: Tests prove cross-owner isolation, capability revocation, logout/refresh
  behavior, expired artifact grants, and read-only default agent sessions.
- AC06: `docs/api-contract.md` contains the authentication matrix and exact delegated-session exchange/revocation
  contract in its Authentication section; `api/tests/auth/test_contract.py` asserts every realm, client, issuer,
  audience, scope, capability, redirect, lifetime, request, response, and error value.


#### Steps

- Add `api/src/muninn/auth` for discovery, JWKS caching, issuer/audience/signature/expiry
  validation, five-minute clock skew, subject mapping, and safe diagnostics.
- Provision Keycloak realm templates for Android, web, CLI device flow, and MCP gateway.
  Register exact redirect URIs; reject wildcard redirects in production.
- Add capability dependencies for read, metadata write, processing administration, and deletion. Declare
  `notes:human:create` and `notes:agent:create` in the shared capability vocabulary,
  but leave all note-route authorization enforcement and tests solely to Task 09.
- Add device-flow client and OS-keyring storage to `api/src/muninn/client`; invalidate
  cached access tokens on logout and revoke sessions server-side where supported.
- Add delegated MCP exchange and revocation endpoints as specified in Technical Notes, then test every session class.
- Run `uv run --project api pytest api/tests/auth/test_contract.py api/tests/auth/test_keycloak.py
  api/tests/auth/test_delegated_session.py` before Task 03 closes.


### 04: Implement API artifact lifecycle and upload admission

Build the authenticated domain API, MinIO manifest boundary, resumable uploads, and server-side
admission controls. The server, not Android's UI, enforces media limits.


#### Acceptance Criteria

- AC01: Authorized clients create, list, inspect, and edit owned conversations
  newest first with immutable capture time and optimistic concurrency.
- AC02: Upload admission rejects before queue dispatch any file exceeding 512 MiB, duration over 3 hours, unsupported
  format, malformed container, failed decode, owner quota, per-owner rate, or active-upload limit.
- AC03: Accepted uploads use stable operation and conversation identities, resume acknowledged parts, verify exact size,
  SHA-256 digest, duration, and decoded media properties, and publish exactly one committed recording manifest.
- AC04: Abandoned, partial, stale, quarantined, and deleted objects are never readable
  through the API. Grants are operation-specific and expire after 10 minutes.
- AC05: A committed source recording remains retained and retrievable while its conversation exists. No server TTL or
  automatic archival removes it; Android's local 30-day cleanup does not change this invariant.
- AC06: Permanent deletion covers every copy described in Technical Notes
  and exposes `deletion_pending`, `deleted`, or `deletion_failed` safely.
- AC07: `README.md` contains no `archive`, `archived`, or archive-command language; its only removal path is confirmed
  whole-conversation deletion. Verify with `! rg -i 'archive|archived'
  README.md` and `api/tests/docs/test_readme_policy.py`.


#### Steps

- Add PostgreSQL connection pooling, readiness, transaction handling, and production
  migration policy. Keep `init_db()` out of production startup.
- Add MinIO adapter configuration with private buckets, server-side encryption, object lock disabled for deletable data,
  content-length and digest verification, multipart cleanup, and version-aware deletion.
- Add media admission using bounded decoding, safe generated names, quotas,
  token-bucket limits, and quarantine that is never provider-dispatched.
- Replace placeholder routes in `api/src/muninn/api.py` with versioned lifecycle, upload, artifact, processing, and
  whole-conversation deletion endpoints. Do not add note persistence, routes, authorization, or tests in this task.
- Test malformed, oversized, over-duration, unsupported, quota-exhausting, repeated,
  interrupted, and deletion-racing uploads with PostgreSQL and MinIO containers.
- Update `README.md` lifecycle language, remove the archive/delete milestone wording, and state whole-conversation
  deletion with no archival. Add deletion-copy tests for live and backup restore workflows.


### 05: Add queue workers, Zen processing, and grounded-summary evaluation

Dispatch committed recordings through durable NATS JetStream consumers to transcription and summarization workers. Keep
Zen credentials server-side, pin English processing, and make provider output versioned and testable.


#### Acceptance Criteria

- AC01: A committed recording creates one logical transcription job despite repeated upload completion acknowledgments.
- AC02: The transcription result is English, labels voices as `Speaker <N>`, records
  provenance, and never performs automatic human identity recognition.
- AC03: Summaries contain the four required fields; each non-empty claim references a transcript evidence span.
  Unsupported claims use `null`/`[]`; uncertain claims use `confidence: uncertain`.
- AC04: The fixture matrix and pass rule reject invented decisions/action items and unsupported fields.
- AC05: Zen timeout, quota, malformed response, bad audio, and infrastructure failures
  preserve source data and expose safe retryable or terminal stage state.
- AC06: Retry and re-summarization select an explicit transcript
  version, are idempotent, and cannot make stale output current.


#### Steps

- Configure NATS 2.10.26 JetStream streams with file-backed persistence, durable consumers, explicit acknowledgment and
  redelivery policies, job IDs, bounded pending messages, dead-letter
  streams, backpressure, and job reconstruction from PostgreSQL.
- Define provider protocols. Implement the Zen API v1 adapter with server-side configuration,
  30-second timeout, payload limits, response validation, and redacted errors.
- Require model IDs and quota profile in the provider manifest. Zen v1 blocks startup when absent.
- Implement claim, heartbeat, retry/backoff, cancellation, source-version checks, and tombstone checks in workers.
- Store normalized transcript and summary artifacts in MinIO with
  provenance and searchable current-version rows in PostgreSQL.
- Add fixtures under `api/tests/fixtures/grounding/` and deterministic
  evaluator tests before changing prompts or provider code.


### 06: Build the authenticated Python client and Typerdrive CLI

Grow the existing CLI into a thin API client. Preserve Typerdrive conventions and make
a future TUI consume the same client package without creating local authority.


#### Acceptance Criteria

- AC01: Device authorization completes with user code and browser verification, handles
  denial, expiry, revoked refresh, and logout, and leaves no token in terminal output.
- AC02: CLI lists and inspects conversations, lifecycle dimensions, transcript, summary, recording
  availability, and processing failures through the API. Task 09 adds note commands and tests.
- AC03: Metadata and speaker-name edits send revision preconditions and
  show a conflict with current values instead of silently overwriting.
- AC04: Retry, re-summarize, and confirmed deletion require server capabilities.
- AC05: No production command reads, writes, or deletes local service database records.


#### Steps

- Add typed HTTP client, token refresh, retry policy, request IDs, safe error
  translation, and keyring integration under `api/src/muninn/client`.
- Replace direct-database commands with API lifecycle commands; remove recording-only delete.
- Add `inspect`, `status`, `metadata`, `retry`, `summarize`, `delete`, and `auth` commands with
  destructive confirmation. Task 09 solely owns note commands and their authorization tests.
- Add HTTP fixture tests for all authentication, conflict, capability, and tombstone outcomes.
- Document the future TUI as a client of this package only.


### 07: Implement Android capture, cache, and deferred sync

Create the Kotlin client with one recording authority, a foreground service, Room durable outbox, WorkManager sync, and
platform-backed encryption. Android is the only client with offline/local audio-cache behavior.


#### Acceptance Criteria

- AC01: App and widget share one state projection and require intentional gestures. Recording states cannot diverge.
- AC02: The fullscreen capture screen has a prominent red record control, elapsed time, a persistent system recording
  indicator, and explicit `ready`, `recording`, and `stopping` states. `CaptureScreenTest.kt` and `CaptureWidgetTest.kt`
  assert control color, label, gesture, elapsed-time progression, indicator persistence, transitions, and widget parity.
- AC03: Capture works offline for two hours and supports explicit 15-minute, 30-minute, and one-hour extensions. Five
  minutes before the active deadline it plays a warning sound distinct from the automatic-stop sound and attempts to
  foreground the app. If Android denies foregrounding, it posts the persistent warning notification and plays the same
  warning sound. At the deadline it plays the distinct automatic-stop sound, stops, and finalizes.
- AC04: The recording journal writes 1 MiB or 5-second encrypted segments plus a manifest before acknowledging each
  segment. A recoverable fault produces a finalized partial recording with a visible reason and one outbox operation; an
  irrecoverable fault produces `attention-required` with retained journal evidence and no false success.
  No fault automatically restarts capture or discards an earlier queued recording.
- AC05: Offline recordings remain in the encrypted outbox until server size/digest acknowledgment.
  Sync resumes from acknowledged parts and never duplicates conversations or jobs.
- AC06: The app exposes local-only, syncing, processing, ready, failed, attention-required, cache size, and total
  footprint. Uncache removes only local audio for a remotely retained conversation.
- AC07: Accepted source recordings remain local for 30 days after server acknowledgment, then clean up automatically.
  Deletion acknowledgment removes all local conversation data and stale operations.
- AC08: Instrumented tests under `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/` prove the fullscreen,
  widget, deadline-warning, foreground-denied fallback, distinct-sound, background, UI-close, process-death, and reboot
  behaviors. `mobile-ui/app/src/test/kotlin/app/muninn/capture/RecordingStateMachineTest.kt` covers the state model.


#### Steps

- Create the Android project with `minSdk 29`, `targetSdk/compileSdk 35`, Kotlin 2.0.21,
  Compose, Room, WorkManager, AndroidX Security Crypto, and encrypted file storage.
- Implement the recording journal, foreground service, elapsed-time projection, persistent system indicator, deadline
  timer, foreground warning attempt, notification fallback, distinct warning and automatic-stop
  sounds, interruption handling, permission handling, and durable finalization.
- Implement app/widget coordination, accessibility labels, confirmation gestures, and the Technical Notes fault matrix.
- Implement ordered WorkManager outbox synchronization with connectivity constraints, bounded backoff, upload
  resume, conflict reconciliation, quota/media attention states, and 30-day cleanup.
- Add review, transcript/summary, metadata, speaker names, processing actions, deletion, cache management, and on-demand
  server recording retrieval. Do not implement note UI, persistence, routes, authorization, client stubs, or note
  tests; Task 09 owns all note work. Never cache web behavior in this client contract.
- Add `CaptureScreenTest.kt`, `CaptureWidgetTest.kt`, `CaptureDeadlineTest.kt`, and `CaptureLifecycleTest.kt` under the
  exact `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/` package path. Cover emulator/network faults,
  foreground-denied behavior, sounds, process death, reboot, permissions, and storage exhaustion.
- Run `make client/android-instrumented` and `mobile-ui/gradlew --no-daemon -p mobile-ui testDebugUnitTest`. Do not
  close Task 07 until these pass. `client/test`, `qa/full`, and `qa` include the instrumented package; Task 12 repeats
  foreground-warning and sound checks on a real device because emulator foreground policy and speaker output can differ.
- Update Task 07's `D-AC` evidence rows to the exact package paths and AC01 through AC08 anchors, then run `make
  qa/traceability`. Re-run `uv run --project api pytest api/tests/commands/test_make_targets.py` to
  prove `client/test`, `qa/full`, and `qa` still depend on Android instrumentation.


### 08: Implement web capture and administration

Build the React web peer client. It captures online through browser microphone APIs, uploads completed media, and never
persists audio in IndexedDB, Cache Storage, service-worker storage, or application-managed files.


#### Acceptance Criteria

- AC01: An authenticated browser starts and stops capture with microphone permission even when offline. Upload requires
  usable connectivity; an offline or failed upload shows a visible failure and retains the Blob only in memory for
  retry. Navigation, reload, or explicit discard releases it. No browser
  storage, service-worker cache, or application file is used.
- AC02: The web client lists conversations, derivatives, failures, metadata, speaker names, and recording availability
  with Android semantics. Timestamped-note acceptance belongs to Task 09 after the note API exists.
- AC03: Authorized retry, re-summarize, and whole-conversation deletion succeed
  through server capabilities; crafted unauthorized requests fail.
- AC04: `web-ui/tests/capture/offline.spec.ts` proves offline capture, failed/offline upload, in-memory retry, release
  on discard/navigation, release after reload, and no audio bytes in IndexedDB,
  Cache Storage, service-worker caches, or application-managed files.
- AC05: The UI handles conflicts, unavailable artifacts, expired grants,
  and deletion completion without exposing credentials or caching audio.


#### Steps

- Add the Vite/React/TypeScript application and typed API client under `web-ui/` using Keycloak code plus PKCE.
- Implement capture state, permission/error handling, completed upload, conversation list/detail, and
  on-demand audio retrieval using an in-memory `Blob` that is released on navigation.
- Implement metadata conflicts, speaker-name editing, processing actions, deletion confirmation, and capability-based
  controls. Do not implement timestamped-note acceptance in this task; Task 09 owns it after the note API is published.
- Add browser unit/component and Playwright tests for capture and administration, including storage and no-cache checks.
- Run `pnpm --dir web-ui playwright test tests/capture/offline.spec.ts
  tests/admin/permissions.spec.ts` before Task 08 closes.


### 09: Add notes, search, and the bounded MCP adapter

Implement notes before client integration is complete, then expose bounded context and one agent
write operation. MCP never receives database, queue, or object-store credentials.


#### Acceptance Criteria

- AC01: Human clients create and delete notes with a timestamp offset, author identity, creation time, and conversation
  ownership. Offset validation rejects negative values and values beyond known recording duration.
- AC02: Agent notes carry immutable `author_kind=agent`, delegated session ID, and `notes:agent:create`; generic agent
  sessions cannot edit/delete notes or mutate metadata, lifecycle, or deletion state.
- AC03: Search accepts text/date/people/tags, owner-filters before ranking, caps pages at 20,
  uses opaque cursors of 15 minutes, and returns at most 2,000 characters per excerpt.
- AC04: MCP uses Streamable HTTP at `/mcp`, exposes transcript/summary/note resources, bounded search, and
  `add-agent-note` only. A selected conversation retrieval is capped at 12,000 characters per resource and 64 KiB total;
  notes are capped at 50 per response. Responses include conversation ID, artifact version, author kind, and offset.
- AC05: OpenCode and Claude host configurations use the same delegated-session contract. Revocation, expiry,
  pagination, redaction, cross-owner access, and every numeric limit have contract tests.
- AC06: After the note API is published, the web client displays and creates timestamped notes with offset validation;
  it deletes owned human notes and visibly marks agent notes. `web-ui/tests/admin/notes.spec.ts` passes.
- AC07: The Android conversation review UI creates a note at the selected playback offset, displays notes at their
  offsets, visibly marks agent notes, deletes owned human notes, rejects invalid offsets, and handles offline submission
  as an explicit failure without a local note outbox. `NoteScreenTest.kt`
  passes through the instrumented package target.
- AC08: CLI note list/create/delete commands use only the Task 09 routes, preserve offset and
  author-kind fields, and enforce note capabilities. `api/tests/client/test_notes.py` passes.


#### Steps

- Add the note table migration, repositories, service, versioned routes, capability enforcement, deletion semantics, and
  `api/tests/api/test_notes.py`. This task is the sole owner of note persistence, routes, authorization, and tests.
- Add PostgreSQL full-text/search indexes for current excerpts and metadata; remove rows on tombstone.
- Implement `api/src/muninn/mcp` as an authenticated API adapter using Streamable
  HTTP, opaque cursors, fixed limits, and selected-conversation retrieval.
- Implement delegated-session mapping from a host login to `{user_sub, agent_session_id, host, capabilities, issued_at,
  expires_at, revoked_at}`. Issue no shared admin token. Default capabilities
  are `context:read`; the explicit tool requires `notes:agent:create`.
- Add OpenCode and Claude configuration examples without secrets and host-specific contract tests.
- Complete the deferred web, Android, and CLI note integrations only after the note routes pass. Add
  `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/NoteScreenTest.kt`, `web-ui/tests/admin/notes.spec.ts`, and
  `api/tests/client/test_notes.py`, then add all four note evidence paths to the Task 09 traceability row.
- Run `uv run --project api pytest api/tests/api/test_notes.py api/tests/client/test_notes.py
  api/tests/mcp/test_delegated_session.py`, `pnpm --dir web-ui playwright test tests/admin/notes.spec.ts`,
  and `make client/android-instrumented` before Task 09 closes.


### 10: Package Helm, DevSpace, and Cloudflare Tunnel deployment

Package an explicit production-like homelab topology. Use Helm for Kubernetes and DevSpace
for local context; Cloudflare Tunnel configuration is injected only at deployment.


#### Acceptance Criteria

- AC01: `helm lint` and a clean install deploy API, transcription worker, summary worker, MCP gateway, PostgreSQL 18,
  MinIO, NATS 2.10.26, and Keycloak integration with persistence, probes,
  resource limits, service accounts, and network policies.
- AC02: Readiness gates validate PostgreSQL, MinIO, NATS JetStream,
  Keycloak discovery, API, workers, and MCP before release.
- AC03: Cloudflare Tunnel routes only web, API, and MCP public ingress to intended services;
  tunnel credentials are Secret references, never values or files in the repository.
- AC04: `devspace.yaml` starts the same contracts with development credentials
  without weakening production auth, encryption, quotas, grants, or deletion.
- AC05: Values pin microk8s/Kubernetes 1.31 compatibility and the Zen v1 provider
  manifest requires model IDs and quota profile before production readiness.
- AC06: `values-homelab.yaml` supports only embedded PostgreSQL 18, NATS 2.10.26 JetStream with persistent storage, and
  MinIO RELEASE.2025-02-07T23-21-09Z, with external Keycloak. Schema validation and `api/tests/deploy/test_topology.py`
  reject external endpoint, host, URL, or `enabled=false` values for any embedded store.
- AC07: `values-qa-fake-provider.yaml` enables only the deterministic fake provider for deployment QA, and `make
  qa/deploy` installs it after `values-homelab.yaml` without changing store topology or production values.


#### Steps

- Add `infra/helm/muninn` values/templates for workloads, PVCs, migrations, service accounts,
  network policies, secret references, probes, autoscaling, quotas, and private services.
- Implement only chart-managed PostgreSQL, NATS JetStream, and MinIO in `values-homelab.yaml`, each with a named PVC and
  private ClusterIP service. Integrate external Keycloak through the issuer Secret. Add no
  external store endpoint keys, aliases, disabled modes, or alternate production profiles.
- Add `values.schema.json` rules and `api/tests/deploy/test_topology.py` cases that fail on injected PostgreSQL, NATS
  JetStream, or MinIO `endpoint`, `host`, `url`, `external`, or `enabled=false`
  values and assert the fixed internal service names.
- Add `infra/helm/muninn/values-qa-fake-provider.yaml` with exactly `provider.mode: fake`, `provider.fake.fixtureSet:
  deployment-v1`, and `provider.fake.seed: muninn-qa`. Permit it only when `qaDeployment: true`; make
  `qa/deploy` set that value in this file and use the exact install in Project Commands.
- Add the backup fence, manifest snapshot, and retention configuration from Task 11. Add the
  `infra/cloudflared/` template and values that reference an operator-supplied Secret.
- Add `devspace.yaml` and local overlays for hot reload, fixtures, migrations, and dependency readiness.
- Add Helm/Kubernetes/DevSpace smoke tests and document every required non-secret value and secret injection.
- Run `uv run --project api pytest api/tests/deploy/test_topology.py`,
  `make deploy/lint`, and `make qa/deploy` before Task 10 closes.


### 11: Implement observability, backup, restore, and deletion recovery

Make failures diagnosable without content leakage and prove deletion survives
live cleanup, queue reconstruction, backup restore, and stale retries.


#### Acceptance Criteria

- AC01: Logs, traces, metrics, and errors contain only safe IDs and operational dimensions, never audio,
  transcript, summary, personal metadata values, credentials, or signed locations.
- AC02: Operators can distinguish API, database, MinIO, Keycloak, NATS JetStream, transcription, and summary failures
  and inspect backlog age, stage, retry state, storage pressure, and deletion lag.
- AC03: Encrypted, access-controlled backups cover PostgreSQL, MinIO object/version data, Keycloak configuration, and
  deployment state. `restic 0.17.3` stores encrypted PostgreSQL `pg_dump --format=custom`, the quiesced MinIO PVC and
  version/delete-marker inventory, Keycloak realm export, and Helm values/manifests. Daily backups retain 35 days,
  weekly backups retain 12 weeks, and deletion markers expire 400 days
  after tombstone creation, after the final governed backup expires.
- AC04: Restore reconciles tombstones, manifests, versions, search rows, jobs, and deletion markers. It
  never resurrects pre-restore deletions or dispatches work for tombstoned sources.
- AC05: Recovery resumes incomplete synchronization and processing safely after verification, and deletion
  exposes a failed outcome for operator retry rather than falsely reporting completion.
- AC06: Each backup records one `recovery_point_id`, PostgreSQL WAL fence LSN, MinIO version listing, Keycloak export,
  deployment revision, and immutable `manifest-snapshot.json`. The snapshot is the cross-store authority:
  an object not referenced by it is quarantined, never served, and never dispatched.
- AC07: Backup creation uses one write-quiescence fence and one exported PostgreSQL repeatable snapshot. New mutating
  calls return HTTP 503 `backup_write_quiesced` with `Retry-After: 30`; reads continue. The bundle
  contains only MinIO version IDs and delete-marker IDs listed while that fence is held.
- AC08: The recovery concurrency test issues upload completion, metadata update, deletion, and worker completion during
  the fence, verifies their retryable responses, retries them after release, and proves the restored database and MinIO
  inventory represent one recovery point without resurrection. Its exact
  path is `api/tests/recovery/test_backup_concurrent_writes.py`.


#### Steps

- Add redaction middleware, correlation IDs, lifecycle metrics, readiness checks, and alerts for backlog, repeated
  stage failures, deletion lag, backup failure, storage pressure, and quota rejection.
- Implement `infra/backup/create.sh` as one bounded protocol. Acquire the Kubernetes backup Lease, pause dispatch, set
  API write admission to quiescing, and drain in-flight database and MinIO mutations for at most 60 seconds. Abort and
  release the Lease on timeout. During the fence, return HTTP 503 `backup_write_quiesced` with `Retry-After: 30` for
  every state-changing API or MCP operation regardless of transport method.
  Keep authenticated GET/HEAD routes and read-only MCP methods available.
- After drain, insert exactly one `recovery_point_id` fence row. Open a read-only, repeatable-read PostgreSQL
  transaction, record `pg_current_wal_lsn()`, call `pg_export_snapshot()`, and keep the exporter open. Run `pg_dump
  --format=custom --snapshot="$PG_SNAPSHOT_ID"` and export manifests, tombstones,
  jobs, and search rows from that same snapshot into `manifest-snapshot.json`.
- While API writes and object mutations remain quiesced, list every MinIO object version and delete marker with `mc ls
  --versions --recursive --json`. Record exact bucket, key, version ID, delete-marker ID, size, digest, and fence ID in
  `minio-inventory.json`, then snapshot the embedded MinIO PVC through the MinIO pod's read-only restic sidecar mount,
  including `.minio.sys` version metadata. Abort if the inventory cannot satisfy every object version referenced by
  PostgreSQL or the PVC snapshot differs from the inventory. This preserves original version and marker IDs.
- Export the Keycloak realm, Kubernetes resource versions, rendered Helm values, and manifests while the Lease is held.
  Compare Keycloak and deployment digests before and after export and abort on concurrent operator change. Close the
  PostgreSQL exporter only after the database dump, manifest, and MinIO inventory are complete; upload with restic, then
  release write admission, dispatch, and the Lease. NATS JetStream messages are not part of the bundle.
- Implement restore as separate fail-closed commands from Project Commands. `make backup/restore` scales API and workers
  to zero and restores PostgreSQL, exact MinIO versions/delete markers, Keycloak, and deployment state. `make
  backup/quarantine` moves every unreferenced object version to `quarantine/<recovery_point_id>/` while preserving
  tombstones. `make backup/verify` checks the fence, snapshot ID, manifest and inventory
  digests, tombstone precedence, quarantine count, and absence of tombstoned jobs.
- Implement `make backup/enable-workers` as the only enable path. Require successful verification, rebuild NATS
  JetStream from PostgreSQL, run deletion verification, restore recorded API and worker
  replica counts, and fail without scaling any workload when a fence or digest differs.
- Run all five backup commands and `uv run --project api pytest api/tests/recovery/test_cross_store_restore.py
  api/tests/recovery/test_backup_concurrent_writes.py api/tests/recovery/test_deletion_expiration.py`. Retain command
  output, inventories, responses, and restored digests under `.artifacts/recovery/<recovery_point_id>/`.


### 12: Complete QA and release validation

Run the release on a production-like stack and real Android device. Treat
every design criterion from `D-AC01` through `D-AC30` as a release gate.


#### Acceptance Criteria

- AC01: `make qa` covers non-live, non-deployed Python, API, workers, Android, web, CLI, MCP, security, grounding
  fixtures, lifecycle tables, upload admission, and traceability. It excludes every `live` and `deployed` pytest test.
- AC02: Android, web, CLI, and MCP contract tests observe the same ownership, revision, artifact, note, processing, and
  deletion semantics, while preserving their explicitly different audio-cache behavior.
- AC03: `make deploy/lint`, `make deploy/install`, `make qa/deploy`, and `make dev/up` pass in clean supported contexts.
- AC04: Manual release checks cover real-device capture, widget safeguards, accessibility, long capture limits, poor
  networks, Android uncache, permission/storage failures, elapsed time, persistent recording indication, warning
  foreground attempt, denied-foreground notification fallback, both distinct
  sounds, web no-cache behavior, and OpenCode/Claude retrieval.
- AC05: `docs/acceptance-traceability.md` has evidence for every design criterion and no unresolved review outcome.
- AC06: `api/tests/deploy/test_release.py -m deployed` authenticates through Keycloak, captures and commits a recording,
  completes fake transcription and summarization, deletes the conversation, and verifies that a stale client or worker
  request cannot resurrect it. `make qa/deploy` runs this scenario and records its JUnit,
  deployment, and flow evidence in the `D-AC30` row of `docs/acceptance-traceability.md`.


#### Steps

- Run `make qa/traceability` when each task lands and reject missing design-criterion
  evidence rather than deferring the matrix to final QA.
- Run all automated suites in dependency order, then run grounding regression and clean-cluster install tests.
- Run `make qa/deploy`; verify that `api/tests/deploy/test_release.py` completes the authenticated capture-to-summary
  and deletion/no-resurrection flow, then record its evidence paths in the `D-AC30` traceability row.
- Run backup/restore and deletion-race exercises, including queue reconstruction and all copy classes.
- Perform manual device and supported-host checks, record evidence paths in the matrix, and fix all release blockers.


## Unknowns

- No execution-blocking technology choice remains open. The queue is NATS 2.10.26 JetStream with persistent storage and
  PostgreSQL job reconstruction; the database is PostgreSQL 18. Kubernetes is 1.31; Helm is 3.16.4;
  DevSpace is 6.3.3; and the Python, Android, web, and Java versions are pinned above.
- Zen model identifiers are deployment-supplied values, not an unbounded design choice. A checked-in provider manifest
  schema and startup preflight require exact transcription and summary model IDs, Zen API v1, English locale, timeout,
  concurrency, and quota profile before production readiness. The release cannot silently select defaults.


## Technical Notes

### Prior review finding closure

The following ledger assigns every prior finding to one owner task, one contract location, and evidence that its steps
explicitly produce or run. A task is not complete until its listed evidence exists.

| Finding | Owner task | Contract location                                                             | Exact evidence path                                                                                                                                                                                                                    | Execution order                                 |
| ------- | ---------- | ----------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- |
| C01     | Task 10    | Task 10 AC06 and `Production topology`                                        | `api/tests/deploy/test_topology.py`; `make deploy/lint`                                                                                                                                                                                | After Tasks 01-09; before Task 11               |
| C02     | Task 01    | `Project Commands` and root `Makefile`                                        | `api/tests/commands/test_make_targets.py`; `uv run --project api pytest api/tests/commands/test_make_targets.py`                                                                                                                       | First; before Tasks 02-12                       |
| C03     | Task 07    | Task 07 AC01-AC08                                                             | `docs/acceptance-traceability.md`; `make qa/traceability`                                                                                                                                                                              | After Task 07 tests; before Task 08             |
| S01     | Task 11    | Task 11 AC07-AC08 and `Backup and restore contract`                           | `api/tests/recovery/test_backup_concurrent_writes.py`; `uv run --project api pytest api/tests/recovery/test_cross_store_restore.py api/tests/recovery/test_backup_concurrent_writes.py api/tests/recovery/test_deletion_expiration.py` | After Task 10; before Task 12                   |
| S02     | Task 09    | Task 09 AC01-AC02                                                             | `api/tests/api/test_notes.py`; `uv run --project api pytest api/tests/api/test_notes.py api/tests/client/test_notes.py api/tests/mcp/test_delegated_session.py`                                                                        | After Task 08; before Task 12                   |
| S03     | Task 09    | Task 09 AC07                                                                  | `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/NoteScreenTest.kt`; `make client/android-instrumented`                                                                                                                        | After note routes; before Task 12               |
| S04     | Task 07    | Task 07 AC02-AC03                                                             | `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/CaptureDeadlineTest.kt`; `make client/android-instrumented`                                                                                                                   | During Task 07                                  |
| S05     | Task 07    | Task 07 AC08 and `client/android-instrumented`                                | `mobile-ui/app/src/androidTest/kotlin/app/muninn/capture/CaptureLifecycleTest.kt`; `make client/android-instrumented`; `uv run --project api pytest api/tests/commands/test_make_targets.py`                                           | During Task 07; `make qa` repeats it in Task 12 |
| S06     | Task 10    | Task 10 AC07 and `qa/deploy`                                                  | `infra/helm/muninn/values-qa-fake-provider.yaml`; `make qa/deploy`                                                                                                                                                                     | After chart lint; before Task 11                |
| S07     | Task 01    | Task 01 command-evidence step and this ledger                                 | `api/tests/commands/test_make_targets.py`; `uv run --project api pytest api/tests/commands/test_make_targets.py`                                                                                                                       | First; rechecked by each owning task            |
| S08     | Task 03    | `docs/api-contract.md` authentication matrix and error table                  | `api/tests/auth/test_contract.py`; `uv run --project api pytest api/tests/auth/test_contract.py`                                                                                                                                       | After Task 01; before Tasks 04, 06, 08, and 09  |
| S10     | Task 02    | `Synchronization state` and `Visibility/deletion state and client projection` | `api/tests/lifecycle/test_cross_dimension_deletion.py`; `api/tests/lifecycle/test_projection.py`                                                                                                                                       | During Task 02; repeated by Task 12             |
| S11     | Task 01    | `Project Commands` and `Pytest marker contract`                               | `api/tests/commands/test_make_targets.py`; `uv run --project api pytest api/tests/commands/test_make_targets.py`                                                                                                                       | First; repeated by Task 12                      |


### Dependency and build inventory

Use these exact files and supported versions. Regenerate only the named lockfiles with the named tool versions.

| Surface              | Exact files                                                                                                                                                                                                                                | Exact versions and lockfiles                                                                                                                                                                                                    |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Python               | `api/pyproject.toml`, `api/uv.lock`, `api/alembic.ini`, `api/migrations/env.py`, `api/migrations/versions/`                                                                                                                                | Python 3.13, uv 0.8.4; `api/uv.lock` is the Python lockfile                                                                                                                                                                     |
| Python runtime       | `api/src/muninn/api.py`, `api/src/muninn/auth/`, `api/src/muninn/domain/`, `api/src/muninn/storage/`, `api/src/muninn/queue/`                                                                                                              | FastAPI 0.115.6, Uvicorn 0.34.0, Pydantic 2.13.3, pydantic-settings 2.7.1, SQLAlchemy 2.0.37, Alembic 1.14.1, psycopg 3.2.3, nats-py 2.9.0, boto3 1.36.11, PyJWT 2.10.1, httpx 0.28.1, python-multipart 0.0.20                  |
| Python project stack | `api/src/muninn/cli/`, `api/src/muninn/client/`                                                                                                                                                                                            | auto-name-enum 4.2.0, loguru 0.7.3, py-buzz 8.1.2, typerdrive 0.9.9, typer-repyt 0.9.2, typer 0.15.2                                                                                                                            |
| Python development   | `api/tests/`, `api/tests/containers/`                                                                                                                                                                                                      | pytest 8.3.4, pytest-cov 6.0.0, pytest-mock 3.14.0, pytest-pretty 1.2.0, testcontainers 4.10.0, Ruff 0.9.7, Ty 0.0.75, Typos 1.29.10                                                                                            |
| Android build        | `mobile-ui/settings.gradle.kts`, `mobile-ui/build.gradle.kts`, `mobile-ui/app/build.gradle.kts`, `mobile-ui/gradle/libs.versions.toml`, `mobile-ui/gradle/wrapper/gradle-wrapper.properties`, `mobile-ui/gradlew`, `mobile-ui/gradlew.bat` | Kotlin 2.0.21, AGP 8.7.3, Gradle 8.9, Java 17, SDK 35, min SDK 29; Gradle dependency locking in `mobile-ui/gradle.lockfile`                                                                                                     |
| Android packages     | `mobile-ui/app/src/main/`, `mobile-ui/app/src/test/`, `mobile-ui/app/src/androidTest/`                                                                                                                                                     | Compose BOM 2025.02.00, activity-compose 1.10.1, lifecycle-runtime-compose 2.8.7, Room 2.7.0, WorkManager 2.10.1, security-crypto 1.1.0-alpha06, Media3 1.5.1, JUnit 4.13.2, AndroidX test 1.6.1, Espresso 3.7.0, MockK 1.13.16 |
| Web build            | `web-ui/package.json`, `web-ui/pnpm-lock.yaml`, `web-ui/tsconfig.json`, `web-ui/tsconfig.node.json`, `web-ui/vite.config.ts`, `web-ui/eslint.config.js`, `web-ui/playwright.config.ts`, `web-ui/index.html`                                | Node 22.14.0, pnpm 10.6.2; `web-ui/pnpm-lock.yaml` is the web lockfile                                                                                                                                                          |
| Web packages         | `web-ui/src/`, `web-ui/tests/`                                                                                                                                                                                                             | React 19.0.0, react-dom 19.0.0, react-router 7.1.5, TypeScript 5.7.3, Vite 6.1.0, Vitest 3.0.5, Playwright 1.50.1, ESLint 9.19.0                                                                                                |
| Deployment           | `infra/helm/muninn/`, `infra/helm/muninn/values-homelab.yaml`, `devspace.yaml`, `infra/cloudflared/`                                                                                                                                       | PostgreSQL 18, NATS 2.10.26 with JetStream, Kubernetes/microk8s 1.31.4, Helm 3.16.4, DevSpace 6.3.3; chart dependencies are vendored or locked in `infra/helm/muninn/Chart.lock`                                                |

Task 01 adds the planned dependency and build surfaces to their component directories and keeps each component lockfile
checked in. NATS JetStream and PostgreSQL 18 are the service queue and database for both local development and
homelab deployment. No category dependency or unpinned Typerdrive stack is acceptable.


### Pytest marker contract

Register both opt-in markers in `api/pyproject.toml` under `[tool.pytest.ini_options]`:

```toml
markers = [
  "live: calls a configured external provider and requires provider credentials",
  "deployed: runs against an explicitly installed Kubernetes release",
]
```

`qa/test` runs exactly `uv run --project api pytest api/tests -m "not live and not deployed"`. Because `qa/full` depends
on `qa/test` and `qa` depends on `qa/full`, ordinary QA cannot select either marker. `qa/provider` is the explicit
opt-in command for `uv run --project api pytest api/tests/provider -m live`. `qa/deploy` is the explicit opt-in command
for `uv run --project api pytest api/tests/deploy/test_release.py -m deployed` after its required deployment. No
unmarked test may call Zen or require a cluster. `api/tests/commands/test_make_targets.py`
asserts all five selection and exclusion contracts.


### Production topology

The only supported production Helm profile is `infra/helm/muninn/values-homelab.yaml`. It embeds PostgreSQL 18, NATS
2.10.26 with JetStream file-backed persistence, and MinIO RELEASE.2025-02-07T23-21-09Z, each with a named PVC and
private ClusterIP service. It integrates the external Keycloak 26.1.2 service at
`identity.keycloak.svc.cluster.local:8080` through the operator-supplied issuer Secret and does not deploy or accept
external replacements for the three embedded stores. The API, workers, and MCP gateway use only these service names.
`api/tests/deploy/test_topology.py` rejects any Helm value that changes store ownership or enables a second profile.
`values.schema.json` rejects external `endpoint`, `host`, `url`, `external`, and `enabled=false` values for PostgreSQL,
NATS JetStream, and MinIO. The non-production `values-qa-fake-provider.yaml`
may change only `qaDeployment` and `provider`; it cannot change topology.

Its complete contents are:

```yaml
qaDeployment: true
provider:
  mode: fake
  fake:
    fixtureSet: deployment-v1
    seed: muninn-qa
```


### Backup and restore contract

Use `restic 0.17.3`, PostgreSQL 18 `pg_dump`/`pg_restore`, MinIO Client `mc RELEASE.2025-02-21T16-00-00Z`, and Keycloak
`kcadm.sh` 26.1.2. One Kubernetes Lease and one `recovery_point_id` define the fence. The API first pauses dispatch,
rejects new mutations with HTTP 503 `backup_write_quiesced` and `Retry-After: 30`, and drains in-flight PostgreSQL and
MinIO writes for at most 60 seconds. Reads remain available. A
drain timeout aborts the backup and restores normal admission.

After drain, the coordinator inserts the fence row and holds one read-only repeatable-read transaction open. It records
`pg_current_wal_lsn()`, exports `PG_SNAPSHOT_ID` with `pg_export_snapshot()`, and supplies that ID to `pg_dump` and
every manifest, tombstone, job, and search export. While writes remain quiesced, it records every MinIO object version
and delete marker in `minio-inventory.json`. A read-only restic sidecar in the MinIO pod snapshots the embedded PVC,
including `.minio.sys`, so original version and marker IDs survive restore. Each inventory row carries bucket, key,
version or marker ID, digest, size, and fence ID. A missing PostgreSQL-referenced
version or inventory/PVC mismatch aborts the bundle.

The coordinator exports Keycloak and Helm/Kubernetes state under the same Lease and aborts if before/after configuration
digests differ. It then closes the PostgreSQL snapshot, stores immutable manifest and inventory digests, uploads the
encrypted restic bundle, and releases admission and dispatch. NATS JetStream messages are never
backed up; verified PostgreSQL job rows rebuild the streams and durable consumers.

Daily bundles retain 35 days and weekly bundles retain 12 weeks. Tombstone and deletion markers remain in every governed
bundle and expire 400 days after tombstone creation, only after the last applicable bundle expires. Restore uses the
five Project Commands in order: create, restore, quarantine, verify, then enable workers. Restore keeps API and workers
at zero replicas, restores PostgreSQL, exact MinIO versions/delete markers, Keycloak, and deployment state, then
quarantines anything absent from `manifest-snapshot.json`. Verification checks the fence, snapshot ID, both digests,
tombstone precedence, and no tombstoned jobs. Only `backup/enable-workers` may rebuild NATS JetStream and restore
replica counts. `api/tests/recovery/test_backup_concurrent_writes.py` proves the fence
under concurrent upload, metadata, deletion, and worker completion attempts.


### Lifecycle state tables

Persist these dimensions independently. Every transition records operation ID, actor subject or worker identity,
server time, and an audit-safe reason. No client invents a state not listed here.


#### Synchronization state

| State                | Entered by                              | Legal next states                                                    | Retry/result                                     |
| -------------------- | --------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------ |
| `local_only`         | Android finalization                    | `syncing`, `attention_required`, `deletion_pending`                  | Offline wait or attention                        |
| `syncing`            | outbox worker                           | `local_only`, `processing`, `attention_required`, `deletion_pending` | Retry or process                                 |
| `processing`         | API commit                              | `ready`, `failed`, `attention_required`, `deletion_pending`          | Bounded retry; visibility tombstone wins         |
| `ready`              | current derivatives                     | `processing`, `attention_required`, `deletion_pending`               | Explicit reprocess                               |
| `failed`             | terminal stage failure                  | `processing`, `deletion_pending`                                     | Retry only with capability and source retained   |
| `attention_required` | conflict, quota, auth, invalid media    | `syncing`, `deletion_pending`                                        | Human action; no infinite retry                  |
| `deletion_pending`   | tombstone accepted from any prior state | none                                                                 | Ordinary synchronization cannot leave this state |

A tombstone accepted while synchronization is `local_only`, `syncing`, `processing`, `ready`, `failed`, or
`attention_required` transitions that row to `deletion_pending` in the tombstone transaction. Ordinary outbox work,
worker completion, retry, and reconciliation cannot leave `deletion_pending`. A failed cleanup or verification sets
visibility to `deletion_failed` while synchronization remains `deletion_pending`. Authorized deletion retry also leaves
synchronization `deletion_pending`. Successful verification removes the
synchronization content row; synchronization has no `deleted` state.


#### Source recording state

| State              | Legal next states                         | Meaning                                                    |
| ------------------ | ----------------------------------------- | ---------------------------------------------------------- |
| `not_started`      | `uploading`, `deletion_pending`           | Conversation exists without source                         |
| `uploading`        | `uploaded`, `invalid`, `deletion_pending` | Parts may be acknowledged but manifest is not readable     |
| `uploaded`         | `committed`, `deletion_pending`           | Whole-object checks passed; commit transaction pending     |
| `committed`        | `deletion_pending`                        | Source is retained indefinitely while conversation exists  |
| `invalid`          | `uploading`, `deletion_pending`           | Admission failed; quarantine is never processed            |
| `deletion_pending` | none                                      | Content remains pending until verification removes its row |


#### Transcript state

| State              | Legal next states                                                      | Retry/result                                            |
| ------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------- |
| `not_requested`    | `queued`, `deletion_pending`                                           | Only committed source may queue                         |
| `queued`           | `running`, `deletion_pending`                                          | Duplicate enqueue returns original job                  |
| `running`          | `committed`, `failed_retryable`, `failed_terminal`, `deletion_pending` | Heartbeat timeout requeues once                         |
| `committed`        | `superseded`, `deletion_pending`                                       | Current only when source version matches                |
| `superseded`       | none                                                                   | Historical provenance only                              |
| `failed_retryable` | `queued`, `deletion_pending`                                           | Capability-gated retry                                  |
| `failed_terminal`  | `deletion_pending`                                                     | Source remains readable; operator/provider fix required |


#### Summary state

| State              | Legal next states                                                      | Retry/result                                        |
| ------------------ | ---------------------------------------------------------------------- | --------------------------------------------------- |
| `not_requested`    | `queued`, `deletion_pending`                                           | Requires a committed transcript version             |
| `queued`           | `running`, `deletion_pending`                                          | Explicit transcript version is part of job identity |
| `running`          | `committed`, `failed_retryable`, `failed_terminal`, `deletion_pending` | Tombstone cancels work                              |
| `committed`        | `superseded`, `deletion_pending`                                       | Only matching transcript version becomes current    |
| `superseded`       | none                                                                   | Historical provenance only                          |
| `failed_retryable` | `queued`, `deletion_pending`                                           | Bounded retry                                       |
| `failed_terminal`  | `deletion_pending`                                                     | Source and transcript remain available              |


#### Visibility/deletion state and client projection

| State              | Legal next state             | Client projection                                                 |
| ------------------ | ---------------------------- | ----------------------------------------------------------------- |
| `active`           | `deletion_pending`           | Conversation visible according to owner authorization             |
| `deletion_pending` | `deleted`, `deletion_failed` | Visible as deleting; no new writes or processing                  |
| `deletion_failed`  | `deletion_pending`           | Visible as deletion failed; only authorized retry                 |
| `deleted`          | none                         | Resource returns `conversation_deleted`; not listed or searchable |

The visibility/deletion dimension is authoritative for deletion. A tombstone transaction sets visibility to
`deletion_pending` and sets every synchronization, source, transcript, summary, note, search, and job row to its
`deletion_pending` form before returning. Cleanup workers may not set any dimension to `deleted`. Only the verification
transaction may set visibility to `deleted`; it then removes content rows, including synchronization rows, and returns
`conversation_deleted` to every client and worker retry. Successful verification does not create a synchronization
`deleted` state. If any governed copy is missing, present, or unverifiable, visibility becomes `deletion_failed` while
synchronization and all other content dimensions remain `deletion_pending`; authorized deletion retry returns
`deletion_retry_accepted`. A failed cleanup never exposes `deleted` and never permits new writes or processing.

For non-deletion display, `attention_required` wins, then `failed`, `processing`, `syncing`, `local_only`, and `ready`.
Android adds `local_only` or `syncing` only before service acknowledgment; server state never implies a local cache.


### Deletion outcome behavior

Deletion first commits the tombstone and `deletion_pending` transactionally. That commit rejects new writes and cancels
compatible queue work. Cleanup is complete only after every row and copy class is checked. The tombstone and a
non-sensitive deletion marker outlive every client retry and configured backup restore window.

| Concurrent item                | Required outcome                                     | Client/worker result                 |
| ------------------------------ | ---------------------------------------------------- | ------------------------------------ |
| Local-only outbox              | Remove local data and stale operations               | `conversation_deleted`; no upload    |
| Upload session/parts           | Abandon and delete parts; reject completion          | `conversation_deleted`; no manifest  |
| Committed source               | Delete all object versions after tombstone           | No source remains after verification |
| Running worker                 | Cancel; ignore completion after tombstone check      | No derivative                        |
| Queue/dead-letter entry        | Remove or exclude during rebuild                     | No post-restore dispatch             |
| Worker scratch/cache           | Delete and verify by conversation ID                 | Failure keeps `deletion_failed`      |
| Rows, notes, manifests, search | Delete content; retain tombstone/outcome             | Not searchable/readable              |
| Stale client/MCP call          | Reject with stable deletion code                     | No resurrection                      |
| Backup copy                    | Encrypt marker, exclude on restore, expire by policy | Cannot recreate data                 |

If any required copy cannot be verified, return `deletion_failed`, alert the operator, retain the tombstone, and retry
cleanup. Never return successful deletion while a governed live or restore-visible copy remains.

Use this exact transition test: `api/tests/lifecycle/test_cross_dimension_deletion.py`. Delete during `local_only`,
`syncing`, `processing`, `ready`, `failed`, and `attention_required`; assert the tombstone transaction and all states,
ordinary synchronization lockout, worker result, stale API/MCP result, successful verification, failed verification,
retry, row removal, and no-resurrection outcome. `api/tests/lifecycle/test_projection.py` asserts that `deletion_failed`
belongs only to visibility/deletion, synchronization remains `deletion_pending`
on failure, and only visibility/deletion can produce `deleted`.


### Grounded-summary fixture and pass criteria

Store versioned fixtures in `api/tests/fixtures/grounding/v1/`. Each fixture has a transcript with stable evidence span
IDs, expected supported claims, expected unsupported decisions/action items, expected speaker labels, and an expected
summary schema. Include at least 12 fixtures: decisions, no-decision, ambiguity, action assignment, contradiction, empty
transcript, multiple speakers, named speakers, dates, negation, unrelated chatter, and malformed provider output.

The normalized summary schema is:

```text
overview: {text, evidence_span_ids, confidence}
key_topics: [{text, evidence_span_ids, confidence}]
decisions: [{text, evidence_span_ids, confidence}]
action_items: [{text, owner, due_date, evidence_span_ids, confidence}]
```

Use `null`/`[]` for unsupported fields and `confidence: uncertain` when the transcript is ambiguous. The deterministic
evaluator passes only when all 12 fixtures validate schema, every supported claim has at least one valid evidence span,
unsupported decisions/action items are absent, no evidence span is outside the selected transcript, supported-claim
precision is at least 0.95, and recall is at least 0.90. A provider or prompt change fails the release gate if any
unsupported claim appears or either metric falls below threshold. Record fixture version, provider, model, prompt
version, metrics, and evaluator output as the release artifact. Two reviewers must find at least 95% agreement on
unsupported-claim suppression in a random 20-fixture monthly sample; disagreement blocks promotion.


### Android interruption fault matrix

| Fault                         | Durable result                         | UI/notification                              | Retry behavior          |
| ----------------------------- | -------------------------------------- | -------------------------------------------- | ----------------------- |
| App backgrounded              | Service and journal continue           | Persistent disclosure; widget reflects state | Continue                |
| UI closed                     | Journal finalizes current segments     | Ready state and finalized notice             | Queue one upload        |
| Process killed/rebooted       | Recover segments; mark tail            | `attention_required` if tail is lost         | Retry partial           |
| Microphone permission revoked | Stop and finalize available segments   | Actionable permission error                  | No restart              |
| Audio interruption            | Pause/stop and journal reason          | Visible interruption state                   | User explicitly resumes |
| Storage exhaustion            | Finalize complete segments if possible | Space warning and cache guidance             | Queue valid partial     |
| Journal corruption            | Preserve evidence; no fabricated media | `attention_required`                         | No duplicate outbox     |


### Authentication and security contracts

| Client/session        | Flow                       | Validation/storage                                         | Capabilities        |
| --------------------- | -------------------------- | ---------------------------------------------------------- | ------------------- |
| Android               | Code + PKCE                | Keycloak issuer/audience, exact redirect, Android Keystore | User capabilities   |
| Web                   | Code + PKCE                | HTTPS exact redirect, no secret, in-memory access token    | User capabilities   |
| CLI/TUI               | Device authorization grant | OS keyring refresh token; access token memory only         | User capabilities   |
| MCP delegated session | Host handoff               | Subject, host, session, expiry, revocation                 | Read; optional note |

The API validates issuer, audience, signature, expiry, `nbf`, and subject. Access tokens expire in 10 minutes; refresh
tokens are rotated and revoked on logout or suspected replay. PKCE uses S256 and rejects wildcard redirects. Delegated
MCP sessions expire after 15 minutes of inactivity and 8 hours maximum, are revocable by the user, and cannot escalate
capabilities. Every artifact grant is operation-specific, owner-scoped, and valid for 10 minutes. TLS terminates at the
Cloudflare Tunnel and remains encrypted to internal services where traffic crosses a trust boundary.

`docs/api-contract.md`, under `Authentication matrix`, is normative. Helm requires `global.publicHostname` as one
lowercase FQDN with no scheme, port, path, query, fragment, wildcard, or trailing dot. It defines
`PUBLIC_ORIGIN=https://${global.publicHostname}`. The chart derives and registers redirects from that value and rejects
independently supplied redirect values. The Keycloak issuer is the exact HTTPS URL in the operator-supplied
`auth.issuer` Secret; every client and API uses that same issuer and `aud=muninn-api`.

| Client      | Realm    | Client ID and type                | Issuer/audience                 | Scopes and capabilities                                        | Exact redirects                       |
| ----------- | -------- | --------------------------------- | ------------------------------- | -------------------------------------------------------------- | ------------------------------------- |
| Android     | `muninn` | `muninn-android`, public          | `auth.issuer`; `aud=muninn-api` | `openid profile email`; user capabilities                      | `com.muninn.app:/oauth2redirect`      |
| Web         | `muninn` | `muninn-web`, public              | `auth.issuer`; `aud=muninn-api` | `openid profile email`; user capabilities                      | `${PUBLIC_ORIGIN}/auth/callback`      |
| CLI/TUI     | `muninn` | `muninn-cli`, public device-flow  | `auth.issuer`; `aud=muninn-api` | `openid profile email offline_access`; user capabilities       | None; verification URI is discovered  |
| MCP gateway | `muninn` | `muninn-mcp`, confidential server | `auth.issuer`; `aud=muninn-api` | `openid profile email`; `context:read`, optional note creation | `${PUBLIC_ORIGIN}/mcp/oauth/callback` |

The same section defines `access_token=600s`, rotated refresh tokens with a 30-day absolute lifetime and 24-hour idle
lifetime, artifact grants of `600s`, five-minute clock skew, delegated session inactivity of `900s`, and delegated
session maximum lifetime of `28800s`. It lists `context:read`, `metadata:write`, `processing:admin`, `deletion:write`,
`notes:human:create`, and `notes:agent:create`; agent sessions start with `context:read` and may receive only the last
capability through explicit delegation. Redirects are exact strings, HTTPS is required for web and MCP, and no wildcard
or client secret is accepted for Android or web. Tokens remain in Android Keystore,
the web memory, CLI OS keyring for refresh only, and the MCP server session store.

The same file defines the response body as `{ "error": { "code": "<stable-code>", "message": "<safe-message>",
"request_id": "<uuid>" } }` and contains this general authentication and capability matrix. Every 401 response includes
`WWW-Authenticate: Bearer`; no row reveals subjects, owners, token
claims, or required capabilities beyond the requested operation.

| Failure                                                  | Stable code                 | HTTP status |
| -------------------------------------------------------- | --------------------------- | ----------- |
| Missing bearer credential                                | `authentication_required`   | 401         |
| Malformed token, bad signature, or unsupported algorithm | `credential_invalid`        | 401         |
| Expired token outside clock skew                         | `credential_expired`        | 401         |
| `nbf` is in the future outside clock skew                | `credential_not_active`     | 401         |
| Issuer differs from `auth.issuer`                        | `credential_wrong_issuer`   | 401         |
| Audience does not contain `muninn-api`                   | `credential_wrong_audience` | 401         |
| Revoked or replayed credential                           | `credential_revoked`        | 401         |
| Authenticated subject lacks the route capability         | `capability_denied`         | 403         |
| Cross-owner resource lookup                              | `resource_not_found`        | 404         |

`api/tests/auth/test_contract.py` renders the chart with `global.publicHostname=muninn.homelab.test`, asserts exact web
and MCP redirects, and rejects malformed hostnames and redirect overrides.
It asserts every code, status, header, and safe body.

The delegated-session contract in `docs/api-contract.md` defines `POST /v1/mcp/sessions/exchange` with a user bearer
token and JSON `{ "host": "opencode|claude", "requested_capabilities": ["context:read", "notes:agent:create"] }`. A 201
response is `{ "session_id": "uuid", "user_sub": "string", "capabilities": ["..."], "issued_at": "rfc3339",
"expires_at": "rfc3339", "inactivity_expires_at": "rfc3339", "mcp_endpoint": "/mcp" }`; it never returns a service
token. `POST /v1/mcp/sessions/{session_id}/revoke` returns 204 and marks `revoked_at`; unknown, cross-owner, expired,
over-capability, malformed, and already-revoked requests return `session_not_found`, `session_forbidden`,
`session_expired`, `capability_denied`, `invalid_request`, and `session_revoked` respectively with HTTP 404, 403, 401,
403, 400, and 409. A revoked session returns `session_revoked` for `/mcp` and cannot be exchanged into another session.
`api/tests/auth/test_contract.py` and `api/tests/mcp/test_delegated_session.py` are the exact evidence paths.


### Upload admission controls

Apply controls before object visibility and before queue dispatch: 512 MiB maximum bytes, 3-hour maximum duration, WAV,
M4A/AAC, and WebM/Opus allowlist, bounded container parsing, decoded-stream validation, SHA-256 and exact byte checks,
safe server-generated names, 10 active uploads per owner, 10 GiB committed source quota per owner, 20 upload starts per
hour per owner, and a deployment-configured global storage watermark. Return stable invalid-media, quota, rate-limit, or
capacity codes. Quarantined objects have no artifact grant and are deleted by cleanup.
Tests cover each limit and prove that rejected media creates no processing job.


### MCP delegated-session and result contract

Use Streamable HTTP at `/mcp` with Keycloak gateway authentication. OpenCode and Claude receive host configuration that
points to the gateway and uses the same user/delegated-session handoff; no host receives a service account. Opaque
cursors encode no content, are signed server-side, expire after 15 minutes, and cannot cross users or queries.

Search returns at most 20 rows, 2,000 characters per excerpt, and 64 KiB total. Selected transcript/summary retrieval
returns at most 12,000 characters per resource and 50 notes. The adapter truncates at evidence-span boundaries, marks
truncation, and requires another page or explicit selection. Every result includes conversation ID, artifact version,
recording offset where applicable, and `author_kind` for notes. Redact credentials, keys, signed URLs, and content. The
only tool is `add-agent-note`; it accepts a conversation, offset,
text, and idempotency key and requires `notes:agent:create`.


### Client authority and retention invariants

Android is authoritative for unacknowledged local outbox work. The service is authoritative after acknowledgment. Web,
CLI, TUI, and MCP never write service databases or object storage directly. Server recordings and derivatives remain
available indefinitely while the conversation exists. Android may remove its accepted local source after 30 days or on
explicit uncache; web never stores an audio cache. Whole-conversation deletion is the only
first-release removal path; there is no archive, archive status, or archival worker.


### Review outcomes

- **C01**: Task 10 selects only embedded PostgreSQL, NATS JetStream, and
  MinIO with external Keycloak. It rejects external store values.
- **C02**: Every Make target has a separate Project Commands subsection with prerequisites, exact command, and output.
- **C03**: Task 07 uses sequential AC01 through AC08, and all Task 07 references use those anchors.
- **S01**: One write-quiescence fence binds the exported PostgreSQL snapshot to exact MinIO versions and delete markers.
- **S02**: Task 09 solely owns note migrations, persistence, routes, authorization, client integration, and tests.
- **S03**: Task 09 adds executable Android note UI acceptance through the exact instrumented package.
- **S04**: Task 07 covers elapsed time, the persistent indicator, foreground warning
  attempt, notification fallback, and distinct warning and stop sounds.
- **S05**: `client/android-instrumented` runs `app.muninn.capture`.
  It is required by `client/test`, `qa/full`, and `qa`.
- **S06**: Deployment QA composes the homelab and checked-in fake-provider values files with one exact install command.
- **S07**: Every closure-ledger file and command is created and run by its named owning task.
- **S08**: `docs/api-contract.md` derives redirects from `global.publicHostname`.
  It fixes auth error codes and statuses.
- **S10**: Synchronization defines `deletion_pending` from every tombstone-eligible state. Ordinary synchronization
  cannot leave it; failure remains pending, and successful verification removes the row without a `deleted` state.
- **S11**: Normal Python QA excludes `live` and `deployed`. Registered markers
  are selected only by the explicit `qa/provider` and `qa/deploy` commands.
