# Implementation journal: Muninn first release

This journal maps the current worktree changes to the approved implementation plan and records verified results and
known gaps without treating the partial release as complete.


## Source plan

`.artifacts/20260804--muninn-conversation-context/implementation-plan.md`


## Status

**Incomplete**: The current monorepo contains the API, CLI, mobile, web, infrastructure, documentation, and test slices,
but the release is not complete.

Generated `.coverage`, `.coverage.xml`, and `muninn.db` are excluded verification artifacts and are not implementation
changes. The final handoff restores tracked coverage files before review.

The latest processing/model refactor removes redundant Conversation artifact object-key projections. API availability
now
derives from current `ArtifactManifest` rows. `CompletionEvent` carries `transcript_version`, and `apply_completion`
validates job kind, source version, and transcript correlation before reading or promoting a result. Focused mismatch
tests
cover those rejection paths. These changes do not resolve the release blockers recorded below.


## Current status and blockers

Execution-review iteration 06 remains **BLOCKED - CHANGES REQUIRED**. The independent review reports critical and
significant findings that are not fixed, especially nullable job uniqueness, stale result promotion, the MinIO read
defect,
incomplete upload and idempotency handling, missing MCP mount and delegation, non-runnable Helm topology, incomplete CLI
clients and Android behavior, unsafe JWT revocation, and non-idempotent deletion.


## Files modified mapping

The lists below assign every implementation file in the current worktree to its primary plan task. Files used by more
than one task are listed under their first or primary owner.


### Task 01

- UPDATED: `.gitignore`
- UPDATED: `Makefile`
- UPDATED: `api/pyproject.toml`
- UPDATED: `api/uv.lock`
- CREATED: `api/alembic.ini`
- CREATED: `docs/acceptance-traceability.md`
- CREATED: `docs/api-contract.md`
- CREATED: `docs/release-contract.md`
- CREATED: `api/tests/traceability/check_matrix.py`
- UPDATED: `api/tests/test_smoke.py`


### Task 02

- CREATED: `api/migrations/env.py`
- CREATED: `api/migrations/script.py.mako`
- CREATED: `api/migrations/versions/0001_release.py`
- UPDATED: `api/src/muninn/models.py`
- UPDATED: `api/src/muninn/schemas/`


### Task 03

- CREATED: `api/src/muninn/auth/__init__.py`
- CREATED: `api/src/muninn/auth/jwt.py`
- CREATED: `api/src/muninn/client/auth.py`
- CREATED: `api/tests/auth/__init__.py`
- CREATED: `api/tests/auth/test_contract.py`


### Task 04

- UPDATED: `api/src/muninn/api.py`
- CREATED: `api/src/muninn/storage.py`
- CREATED: `api/tests/api/__init__.py`
- CREATED: `api/tests/api/test_contract.py`


### Task 05

- CREATED: `api/src/muninn/job_manager.py`
- CREATED: `api/src/muninn/provider.py`
- CREATED: `api/src/muninn/queue/__init__.py`
- CREATED: `api/src/muninn/queue/contracts.py`
- CREATED: `api/src/muninn/queue/nats.py`
- CREATED: `api/src/muninn/worker.py`
- CREATED: `api/tests/provider/__init__.py`
- CREATED: `api/tests/provider/test_contract.py`


### Task 06

- UPDATED: `cli/src/muninn_cli/main.py`
- CREATED: `cli/src/muninn_cli/client/__init__.py`


### Task 07

- CREATED: `mobile-ui/app/build.gradle.kts`
- CREATED: `mobile-ui/app/src/main/AndroidManifest.xml`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/MainActivity.kt`
- CREATED: `mobile-ui/build.gradle.kts`
- CREATED: `mobile-ui/gradle/wrapper/gradle-wrapper.properties`
- CREATED: `mobile-ui/gradlew`
- CREATED: `mobile-ui/settings.gradle.kts`


### Task 08

- CREATED: `web-ui/eslint.config.js`
- CREATED: `web-ui/index.html`
- CREATED: `web-ui/package.json`
- CREATED: `web-ui/pnpm-lock.yaml`
- CREATED: `web-ui/src/main.tsx`
- CREATED: `web-ui/tsconfig.json`
- CREATED: `web-ui/vite.config.ts`


### Task 09

- CREATED: `api/src/muninn/mcp/__init__.py`
- CREATED: `api/src/muninn/mcp/routes.py`


### Task 10

- CREATED: `infra/helm/muninn/Chart.yaml`
- CREATED: `infra/helm/muninn/templates/config.yaml`
- CREATED: `infra/helm/muninn/templates/deployment.yaml`
- CREATED: `infra/helm/muninn/templates/topology.yaml`
- CREATED: `infra/helm/muninn/values-homelab.yaml`
- CREATED: `infra/helm/muninn/values-qa-fake-provider.yaml`
- CREATED: `infra/helm/muninn/values.schema.json`
- CREATED: `infra/helm/muninn/values.yaml`
- CREATED: `devspace.yaml`


### Task 11

- CREATED: `infra/backup/create.sh`
- CREATED: `infra/backup/enable-workers.sh`
- CREATED: `infra/backup/restore.sh`
- CREATED: `infra/backup/verify-restore.sh`


### Task 12

- UPDATED: `README.md`
- UPDATED: `api/src/muninn/config.py`
- CREATED: `api/tests/e2e/__init__.py`
- CREATED: `api/tests/e2e/test_release.py`
- CREATED: `web-ui/playwright.config.ts`
- CREATED: `web-ui/tests/capture/offline.spec.ts`

The approved implementation plan and execution review are process inputs, not implementation output. This journal is
the execution record:

- PRE-EXISTING UPDATED: `.artifacts/20260804--muninn-conversation-context/implementation-plan.md`
- REVIEW INPUT: `.artifacts/20260804--muninn-conversation-context/execution-review--whole-plan--01.md`
- UPDATED: `.artifacts/20260804--muninn-conversation-context/implementation-journal.md`


## Tasks

### Task 01: Establish release contracts, toolchains, and traceability

**Status: Partial.**

Added contracts, marker registration, lockfiles, the command graph, traceability checks, and focused command tests.
Markdown validates `README.md` and every file under `docs/`. It excludes dependencies, the license, and historical
workflow artifacts.


#### Verification

- `make qa/markdown`: passed for four project-authored files.
- Component Make targets are validated by executing them directly; no unit test asserts Makefile text.
- `make -n backup/create backup/restore backup/verify backup/enable-workers RECOVERY_POINT_ID=test-point`: passed and
  printed the approved four-target sequence.
- `make qa/traceability`: passed, 30 complete rows, in the earlier resumed execution.


#### Blockers

- The Android dependency lockfile and several plan-named full contract suites remain absent.
- The release command graph exists, but prerequisites for every target are not available on this host.

----

### Task 02: Define persistence, lifecycle state, and deletion behavior

**Status: Partial.** Added the initial migration and control-plane entities. Redundant Conversation artifact object-key
projections were removed; API availability derives from current `ArtifactManifest` rows. Focused API tests cover
revision
conflicts, note validation, whole-conversation deletion, and tombstone precedence.


#### Verification

- `uv run pytest api/tests/api/test_contract.py -m unit`: passed, three tests as part of the focused run.


#### Blockers

- Clean PostgreSQL 18 migration, transition, constraint, and restore tests remain unimplemented or unverified.
- Current models still include transitional deletion states beyond the approved simple active/deleted projection.

----

### Task 03: Implement Keycloak authentication and authorization contracts

**Status: Partial.** Added claim validation, JWKS signature admission, and stable error-code tests for issuer, audience,
subject, expiry, and not-before behavior.


#### Verification

- `uv run pytest api/tests/auth/test_contract.py -m unit`: passed, six tests as part of the focused run.


#### Blockers

- Persistent and issuer-scoped revocation, production Keycloak fixtures, PKCE, delegated sessions, and complete device
  authorization remain incomplete.

----

### Task 04: Implement API artifact lifecycle and upload admission

**Status: Partial.** Added basic owned conversation routes, bounded single-request recording admission, basic operation
creation, MinIO adapter scaffolding, deletion, and stale-create rejection after tombstoning.


#### Verification

- `api/tests/api/test_contract.py::test_revision_conflict_returns_current_values`: passed.
- `api/tests/api/test_contract.py::test_tombstone_prevents_stale_conversation_recreation`: passed.


#### Blockers

- Full resumable upload, media decode and duration checks, quotas, grants, MinIO integration, and deletion-race coverage
  remain incomplete.

----

### Task 05: Add queue workers and Zen processing

**Status: Partial.**

Added versioned NATS contracts, scoped Object Store protocols and fakes, a MinIO-to-NATS outbox dispatcher, NATS-only
worker processing, deterministic fake-provider transcription and summarization, verified result promotion, and summary
queueing after transcript completion. `CompletionEvent` now carries `transcript_version`; `apply_completion` validates
job
kind, source version, and transcript correlation before accepting a result. Focused mismatch tests cover these rejection
paths. Malformed messages are terminated, and mismatched completions cannot claim a conversation ready. Stale but
otherwise
correlated results remain a release blocker.


#### Verification

- `make -C api test`: 27 passed, 1 deselected; emitted five botocore deprecation warnings.
- `make -C api lint`: passed.
- `make -C api types`: passed with Ty's pre-release warning.


#### Blockers

- Live NATS, MinIO, PostgreSQL, and free Zen integration remain unverified. Production provider retry policy and
  deployment wiring still require integration evidence.

----

### Task 06: Build the authenticated Python client and Typerdrive CLI

**Status: Partial.** Added an HTTP-client surface and replaced part of the scaffold CLI behavior.


#### Verification

- `uv run muninn --help`: passed in the earlier resumed execution.


#### Blockers

- Complete device authorization, keyring persistence, lifecycle commands, conflict handling, and plan-named client tests
  remain incomplete.

----

### Task 07: Implement Android capture, cache, and deferred sync

**Status: Partial.** Added the first practical capture slice: a deterministic state machine, fullscreen Compose capture
screen, foreground service, deadline extensions, notification indicator, widget entry point, local-store boundaries, and
unit tests for state transitions and widget hold protection.


#### Verification

- No Android build or unit test passed on this host because the checked-in `gradlew` delegates to `gradle`, which is not
  installed or executable here. No Android SDK or API 35 emulator is available.


#### Blockers

- The Android SDK, Gradle installation, and API 35 emulator are unavailable, so compilation remains unverified.
- The service currently records a single MediaRecorder file. Encrypted 1 MiB/5-second journal segments, durable Room
  persistence, WorkManager deferred upload, cache cleanup, fault recovery, notification foreground-denied fallback, and
  the exact instrumented lifecycle suite remain incomplete.


#### Android slice files

- UPDATED: `mobile-ui/app/build.gradle.kts`
- UPDATED: `mobile-ui/app/src/main/AndroidManifest.xml`
- UPDATED: `mobile-ui/app/src/main/java/app/muninn/capture/MainActivity.kt`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/CaptureCoordinator.kt`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/CaptureWidget.kt`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/LocalStore.kt`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/RecordingService.kt`
- CREATED: `mobile-ui/app/src/main/java/app/muninn/capture/RecordingState.kt`
- CREATED: `mobile-ui/app/src/main/res/layout/capture_widget.xml`
- CREATED: `mobile-ui/app/src/main/res/values/styles.xml`
- CREATED: `mobile-ui/app/src/main/res/xml/capture_widget_info.xml`
- CREATED: `mobile-ui/app/src/test/kotlin/app/muninn/capture/CaptureWidgetTest.kt`
- CREATED: `mobile-ui/app/src/test/kotlin/app/muninn/capture/RecordingStateMachineTest.kt`

----

### Task 08: Implement web capture and administration

**Status: Partial.** Added the Vite/React project, a minimal microphone capture control, pinned web dependencies, and
typecheck, lint, and build configuration.


#### Verification

- `corepack pnpm@10.6.2 --dir web-ui typecheck`: passed.
- `corepack pnpm@10.6.2 --dir web-ui lint`: passed.
- `corepack pnpm@10.6.2 --dir web-ui build`: passed.


#### Blockers

- Authentication, upload, administration, conflicts, artifact retrieval, and web note behavior remain unimplemented.
- The current UI does not retain a completed audio blob for upload or retry.

----

### Task 09: Add notes, search, and the bounded MCP adapter

**Status: Partial.** Added basic human-note API routes and bounded MCP context and `add-agent-note` routes.


#### Verification

- Focused note input validation passed in `api/tests/api/test_contract.py`.


#### Blockers

- Delegated-session exchange and revocation, search, opaque cursors, host interoperability, and client note integrations
  remain incomplete.

----

### Task 10: Package Helm and DevSpace deployment

**Status: Partial.** Added the homelab chart, embedded PostgreSQL, MinIO, and NATS topology, external Keycloak settings,
fake-provider smoke overlay, and DevSpace scaffold.


#### Verification

- `make deploy/lint`: passed Helm lint and both render paths in the earlier resumed execution.


#### Blockers

- No clean-cluster install, readiness integration, topology test suite, or DevSpace smoke run completed.

----

### Task 11: Implement basic operations and recovery

**Status: Incomplete.** Retained only the approved `backup/create`, `backup/restore`, `backup/verify`, and
`backup/enable-workers` graph. Removed the unapproved quarantine target and script.


#### Verification

- `make -n backup/create backup/restore backup/verify backup/enable-workers RECOVERY_POINT_ID=test-point`: passed and
  showed restore before verification and verification before worker enablement.


#### Blockers

- The scripts now provide an operator-run PostgreSQL, MinIO, and JetStream PVC backup/restore skeleton, but real cluster
  execution and deletion-race verification remain unimplemented.

----

### Task 12: Complete QA and release validation

**Status: Incomplete.**

Added the exact Python `api/tests/e2e/test_release.py` entry point plus minimal Playwright configuration and capture
tests.
The Python flow checks readiness, authenticated create/upload, process completion, deletion, and stale recreation
rejection. It requires deployment-selected free Zen model IDs and evaluates lifecycle completion, not model prose
quality.


#### Verification

- Narrow processing/API unit tests: passed, 13 tests.
- `make -C api lint`: passed.
- `make -C api types`: passed with Ty's pre-release warning.
- `uv run pytest api/tests/e2e -m e2e --collect-only`: passed and collected one E2E test.
- Playwright configuration started the Vite server and discovered two tests. Execution stopped because the Chromium
  browser binary is not installed on this host.


#### Blockers

- This host has Node.js 20.20.2 rather than the required Node.js 22.14.0. Web checks were run with pinned pnpm 10.6.2,
  but the canonical Node 22 command remains unverified here.
- Chromium, Android, PostgreSQL, NATS, MinIO, Keycloak, free Zen credentials, and a Kubernetes E2E context are
  unavailable.
- The Python E2E flow cannot pass until a real service stack and configured free Zen models are available.
- No manual device or supported-agent-host release evidence exists.


## Overall verification

| Command                                         | Result                                                               |
| ----------------------------------------------- | -------------------------------------------------------------------- |
| `make qa/markdown`                              | Passed for `README.md` and all three files under `docs/`.            |
| `make -C api test`                              | 27 passed, 1 deselected; emitted five botocore deprecation warnings. |
| `make -C api lint`                              | Passed.                                                              |
| `make -C api types`                             | Passed with Ty's pre-release warning.                                |
| Narrow processing/API unit tests                | Passed, 13 tests.                                                    |
| Web typecheck, lint, and build with pnpm 10.6.2 | Passed.                                                              |
| Python E2E collection                           | Passed, one test collected.                                          |
| Playwright execution                            | Blocked by missing Chromium after discovering two tests.             |
| Full `make qa`                                  | Not run because Android and Node 22 prerequisites are unavailable.   |

No commit was created.
