# Design Plan: Muninn conversation context platform

Muninn captures in-person conversations reliably, turns them into durable knowledge, and makes that knowledge available
to Tucker and his software agents without requiring a cloud-first workflow.


## Goal

Build an offline-first Android capture experience and an authenticated web app backed by a self-hosted conversation
service. A user can start or stop recording with one prominent action, trust that a recording survives loss of
connectivity, and later review, organize, or remove it from the mobile or web experience. The Android app owns
synchronization initiation: it waits while offline and starts or retries sync when a usable data connection becomes
available. The homelab service owns transcription, summarization, metadata, retention, and access control. The web app
adds administrative controls, while the Python command-line application and agent integrations expose the same
conversation lifecycle.

The design extends the current Typerdrive-based Python scaffold rather than replacing it. The existing service boundary,
conversation ownership model, metadata concepts, artifact separation, and CLI become the foundation for a production
system. Android is a separate client, while future terminal and agent experiences remain clients of the same service.


### Requirements

#### Mobile app

- `R01`: The Android app is a fullscreen experience with one prominent red record control whose state clearly
  distinguishes ready, recording, and stopping.
- `R02`: A home-screen widget extends the Android app with a similar large record control, reflects the app's active
  recording state, and protects against accidental triggers before starting or stopping capture.
- `R03`: Capture works without network access and persists recordings plus pending changes in a durable local outbox.
  After server acceptance, the Android app retains the local source recording for 30 days, then cleans it up
  automatically. The app can load the recording from the server on demand while it remains available there.
- `R04`: Synchronization resumes after interruption without duplicating conversations or corrupting recordings.
- `R06`: The mobile app lists conversations and displays transcripts, summaries, processing state, failures, and local
  recording cache state.
- `R07`: The mobile app shows the cached-recording disk footprint for each conversation and in total, and lets the user
  uncache any remotely retained recording without deleting it from the server.
- `R08`: Users can edit title, description, people, and tags from mobile and terminal clients. Android captures the
  recording timestamp at recording time, and it remains immutable.
- `R20`: Transcripts identify distinct speakers as `Speaker <N>`. The Android and web apps can associate optional human
  names with those speaker labels. Muninn does not automatically identify speakers as real people.
- `R18`: The Android and web apps can create, display, and delete notes attached to a conversation. Each note can
  reference a timestamp in the recording, and clients show notes at their referenced timestamps.
- `R17`: The Android app exposes the same online conversation and administrative functionality as the web app, including
  retrying processing, forcing re-summarization, and permanently deleting conversations. Authorization controls which
  users can perform administrative actions on either client.


#### Web app

- `R16`: An authenticated web app can record conversations through the browser when microphone access is available. It
  provides the same conversation, metadata, transcript, summary, lifecycle, and administrative functionality as the
  Android app, without Android's offline capture, deferred synchronization, or local audio caching capability. The web
  app fetches recorded audio from the server on demand and does not persist audio locally.


#### API and processing platform

- `R05`: The API dispatches synchronized recordings for transcription and produces useful AI summaries from completed
  transcripts.
- `R19`: The API stores timestamped notes with conversation ownership, deletion, and authorization semantics. Notes from
  external agents are marked as agent notes.
- `R09`: Keycloak authenticates clients; PostgreSQL stores metadata; MinIO or compatible S3 storage holds large
  artifacts.
- `R10`: The API can permanently delete a conversation, including its recording, transcript, summary, metadata, and
  other artifacts. Recordings are retained indefinitely by default unless the user deletes the entire conversation.


#### CLI and TUI

- `R11`: A Typerdrive CLI supports inspection, synchronization status, metadata changes, and confirmed conversation
  deletion.
- `R12`: A future TUI can provide the same lifecycle operations without creating a separate domain or data model.


#### Agent integration

- `R13`: OpenCode, Claude, GitHub Copilot, and similar tools can discover and retrieve authorized conversation context.


#### Deployment and security

- `R14`: Operational deployment, backup, diagnostics, and recovery fit the homelab Kubernetes environment. The initial
  deployment is packaged as Helm charts.
- `R15`: Sensitive audio and conversation text remain private by default and never appear in routine telemetry.

Recording playback, collaborative sharing, live transcription, call interception, and fully automatic participant
identity recognition are outside the first release. The platform may add them later without changing conversation
ownership or artifact lifecycle semantics.

----

## Acceptance Criteria

### Capture and offline behavior

#### AC01: App capture is unambiguous

When the user opens the Android app, it presents a fullscreen capture experience with a prominent red control. The
control starts capture, changes to a stop action while recording, and shows elapsed time plus an ongoing system
recording indicator. The app does not require a data connection to start or stop capture. The initial maximum duration
is two hours. Five minutes before that deadline, the app warns the human with a distinct warning sound and offers
extensions of 15 minutes, 30 minutes, or one hour. Selecting an extension moves the deadline by that amount and causes
the same warning to occur five minutes before the new deadline. Dismissing the warning or not responding leaves the
current deadline unchanged. At the deadline, the app plays a different automatic-stop sound, stops and finalizes the
recording, returns to its ready state, and queues the completed recording locally.


#### AC02: Widget controls the same recording

The home-screen widget extends the app with a similar large record control, reflects state changes made in the app, and
cannot create two simultaneous recordings. Starting or stopping from the widget requires an intentional safeguard,
such as a confirmation gesture or press-and-hold interaction, so an incidental home-screen touch does not change
recording state.


#### AC03: Capture survives connectivity loss

When the device has no usable network, capture starts and stops normally and the completed recording remains queued
locally until synchronization succeeds.


#### AC04: Backgrounding and closure have distinct behavior

Sending the app to the background does not stop an active recording. The foreground service continues capture and the
widget reflects its state. Five minutes before the active deadline, the app attempts to return to the foreground so the
human can see the warning and extension choices. If Android prevents foregrounding, the app uses the warning
notification and distinct warning sound instead. Closing the app UI while capture is active stops and finalizes the
recording immediately; reopening never restarts it. The finalized recording and synchronization state remain
recoverable, and the app queues it for API synchronization when a usable data connection is available. If the operating
system reclaims the app or the device restarts, any recoverable active recording is finalized without automatic restart
on app reopen. The foreground recorder enforces the active maximum duration, starting at two hours and extending only
through an explicit 15-minute, 30-minute, or one-hour user choice. It plays the distinct automatic-stop sound when the
active deadline is reached.


#### AC05: Local exhaustion fails safely

If microphone access is unavailable before capture, the app does not claim to be recording and gives the user an
actionable explanation. If local storage becomes exhausted during capture and recording cannot continue, the app stops
and finalizes the partial recording, notifies the user that capture stopped because of insufficient space, and queues it
for synchronization when a usable data connection is available. The notification encourages the user to uncache local
copies of already synchronized recordings, removing their device data while preserving those recordings on the remote
service. A failure never overwrites or discards an earlier queued recording.

----

### Synchronization and processing

#### AC06: Repeated synchronization is idempotent

Retrying any create, metadata change, upload completion, or deletion operation produces the same durable result as one
successful attempt and does not create duplicate conversations or processing jobs.


#### AC07: Interrupted uploads resume

After a connection loss, application restart, or server timeout, synchronization resumes from acknowledged progress and
the server accepts an artifact only after its size and integrity are verified.


#### AC08: Outbox state is visible

The mobile app distinguishes local-only, syncing, processing, ready, failed, and attention-required conversations and
allows retry of recoverable failures without a new recording.


#### AC09: Processing produces durable derivatives

After a recording is accepted, the server creates an English transcript with distinct speakers labeled `Speaker <N>` and
then a summary containing an overview, key topics, decisions, and action items. Each accepted result is associated with
its source and processing version. The Android and web apps can assign optional human-provided names to speaker labels;
automatic identity recognition is not performed.


#### AC10: Processing failures preserve source data

When transcription, summarization, or an infrastructure dependency fails, the conversation remains accessible, the
recording remains eligible for retry, and the user sees which stage failed without seeing internal secrets.


#### AC11: Concurrent metadata edits are not silent

When a client submits changes based on stale metadata, the service does not silently overwrite newer values. It returns
the current state so the client can preserve or explicitly replace its pending edit.

----

### Review and lifecycle

#### AC12: Conversation browsing works on mobile

An authenticated user can browse conversations newest first, inspect synchronization or processing status, and open the
available transcript and summary without downloading the original recording. Each conversation indicates whether its
recording is cached locally and shows the local cached size when present.


#### AC13: Mobile cache management is explicit

The mobile app shows the total disk footprint of cached recordings and the cached size for each conversation. For any
recording that remains available on the server, the user can explicitly uncache the local copy. Uncaching removes only
the device copy, preserves the remote recording and all other conversation data, and leaves the conversation available
for on-demand re-download.


#### AC14: Metadata is editable across clients

Changes to title, description, people, tags, and optional speaker names made by an authorized mobile or web client
become visible to other clients after synchronization. The recording timestamp is captured by Android when recording
begins or is finalized, is accepted as immutable capture metadata by the service, and cannot be edited by any client.


#### AC15: Timestamped notes are shared across clients

The Android and web apps can create a note at a selected recording timestamp, show notes at their referenced timestamps
while reviewing a conversation, and delete notes. Notes remain associated with the conversation and are removed when the
conversation is deleted. Notes created through an external agent are visibly marked as agent notes.


#### AC16: Conversation deletion removes all data

After explicit confirmation, deleting a conversation permanently removes every server-side copy governed by Muninn,
including the recording, transcript, summary, metadata, and other artifacts. Recordings remain retained indefinitely
until this operation is accepted.


#### AC17: Conversation deletion cannot be undone by a retry

After the service accepts a conversation deletion, delayed uploads, processing retries, and stale outbox operations
cannot restore any deleted data or create new derivatives from it.


### CLI, TUI, and agent access

#### AC18: Mobile and web clients have functional parity

The Android and web apps can start and stop capture, upload recordings, browse conversations, view transcripts and
summaries, edit metadata, create and delete timestamped notes, inspect processing status, retry recoverable work, force
re-summarization, and permanently delete conversations. Android additionally supports offline capture, local audio
caching, uncache controls, and connectivity-triggered deferred synchronization. The web app fetches recorded audio on
demand and never caches it locally. The server enforces capabilities for administrative operations on both clients;
hiding a control in either UI is not an authorization boundary.


#### AC19: CLI covers the lifecycle

The Typerdrive CLI can authenticate, list and inspect conversations, report processing and synchronization state, edit
metadata, and request confirmed conversation deletion.


#### AC20: Terminal clients use service authority

CLI and future TUI operations authenticate through Keycloak's device authorization grant, requiring the user to complete
authorization on a separate browser-capable device. They use the same authenticated service behavior as mobile clients
and do not maintain a competing authoritative conversation database. The service validates the resulting access and
refresh tokens and applies the user's normal capabilities.


#### AC21: Agents can retrieve bounded context

A supported agent can search conversations by text, date, people, or tags and retrieve transcript, summary, and notes
under the user's authorization without receiving object-store credentials. Agents cannot mutate metadata or lifecycle
state, and notes they create are explicitly marked as agent notes.


#### AC22: Agent mutations require explicit authority

Read-only agent access is the default for transcripts, summaries, and notes. Agents may create notes through an explicit
note operation, but every such note is marked as an agent note. Agents cannot edit or delete metadata, lifecycle state,
or notes unless a future capability explicitly grants that operation. Destructive conversation deletion is unavailable
to generic agent sessions.


#### AC23: MCP interoperability is demonstrated

MCP is the integration protocol for Muninn because Muninn provides durable context and narrowly scoped tools rather than
agent-to-agent coordination. The MCP adapter works with at least OpenCode and one of Claude or GitHub Copilot, while the
contract remains usable by other conforming MCP clients without vendor-specific conversation semantics. It exposes
authorized transcript, summary, and note resources, conversation search, and an explicit add-agent-note tool. It does
not expose conversation deletion, metadata mutation, or arbitrary database and object-store access.

----

### Security and operations

#### AC24: Identity and ownership are enforced

The service rejects missing, expired, incorrectly issued, or incorrectly targeted credentials and never returns one
user's conversations or artifacts to another user.


#### AC25: Artifact access is constrained

Clients receive only short-lived, operation-specific artifact access. Listing storage contents, guessing object names,
or retaining an expired grant does not provide access to conversation data.


#### AC26: Sensitive content stays out of telemetry

Routine logs, traces, metrics, and error reports contain identifiers and operational dimensions but not audio,
transcript text, summaries, bearer credentials, or signed artifact locations.


#### AC27: Service degradation is diagnosable

Operators can distinguish API, database, object storage, identity, queue, transcription, and summarization failures and
can determine backlog age, failed stage, retry state, and storage pressure for each affected conversation.


#### AC28: Recovery preserves lifecycle invariants

A documented restore exercise recovers metadata and retained artifacts to a consistent point, does not resurrect
conversations deleted before that point, and allows incomplete synchronization or processing to resume safely.

----

### Verification

#### AC29: Offline and destructive paths are exercised

Automated system tests cover offline capture, interrupted and repeated synchronization, stale metadata updates,
processing retries, authorization boundaries, and conversation deletion during each processing stage.


#### AC30: Homelab deployment is reproducible

A clean supported homelab Kubernetes environment can install the Helm charts, deploy the service dependencies, validate
identity and storage integration, pass readiness checks, and complete capture-to-summary and delete-conversation
scenarios.

----

## Architecture

Muninn separates capture, control-plane metadata, large artifacts, asynchronous processing, and context delivery. The
service is authoritative once an operation is acknowledged, while the Android outbox is authoritative for unacknowledged
local work. All clients use a common authenticated domain API rather than coupling to PostgreSQL, MinIO, or workers.

```text
 Android app and widget                 Web app, CLI, future TUI, MCP clients
           |                                        |
           +---------- authenticated domain API ----+
                               |
              +----------------+----------------+
              |                |                |
         PostgreSQL      S3 artifact store   durable work queue
                                                 |
                                         API provider adapter
                                                 |
                                       transcription -> summary
```


### Client experiences

The Android app and web app both capture audio. Android owns the durable mobile outbox. Its app and widget coordinate
through one recording authority so capture state cannot diverge. Android's foreground recording disclosure remains
visible for the entire capture. Android continues to work from a local projection when disconnected, then reconciles
acknowledged server state after connectivity returns. The web app uses browser microphone APIs and requires a usable
data connection to upload completed recordings.

The current Typerdrive Python application grows into the operational CLI and shared service client. It retains the
project's established configuration, error handling, terminal output, and quality conventions. The future TUI composes
the same client capabilities and does not bypass server authorization or lifecycle rules.

The Android and web apps are peer clients of the same domain API and expose the same online functionality. Android adds
the foreground recorder, durable local outbox, local recording cache, uncache controls, and connectivity-triggered
synchronization. The web app records through browser microphone APIs and uploads completed recordings through the API.
It fetches server recordings on demand and never persists audio locally. Both clients expose administrative controls
when the authenticated user has the required server-issued capabilities. Administrative actions are audited, and
neither client writes directly to PostgreSQL, object storage, or the work queue.


### Service boundaries

The domain API owns conversation identity, ownership, metadata revisions, timestamped notes, artifact manifests,
processing state, retention state, administrative operations, and authorization decisions. PostgreSQL is the source of
truth for these records and for deletion tombstones. It stores references and integrity metadata, not large audio or
generated documents.

An S3-compatible artifact store holds immutable recording uploads and versioned transcript and summary artifacts.
Clients never treat an object key as authority. Artifact availability is determined by the service's committed manifest,
which prevents partial uploads and abandoned worker output from appearing as valid content.

A durable work queue decouples accepted recordings from transcription and summarization provider calls. The API owns a
provider adapter boundary and dispatches content to configured external AI providers because the homelab does not host
AI models. Zen API is the first-release provider. Provider credentials remain server-side. Each stage records its
provider, model, input version, output version, attempts, and terminal or retryable outcome. Summarization starts only
from a committed transcript. Workers may repeat work safely, but only the result matching the active source version
becomes current.

Notes are small, queryable PostgreSQL records containing conversation identity, recording timestamp offset, note text,
author identity, author kind, creation time, and deletion state. User-created notes are authored by a human identity;
notes created through agent integrations are authored by the delegated agent session and marked as agent notes. They are
included in conversation retrieval and searchable context, and are deleted with the conversation.

Forcing re-summarization creates an explicit operation against the selected committed transcript version. The API checks
the caller's administrative capability, records the request, and queues one idempotent summary job. It never mutates or
deletes the source recording or transcript as a side effect.


### Data lifecycle

1. The device creates a stable conversation identity before capture and writes audio into protected local storage.
2. Stopping capture finalizes local integrity metadata and appends an upload operation to the durable outbox.
3. When a usable data connection is available, the Android sync worker creates or reconciles the server conversation,
   transfers the recording, and commits its manifest. While offline, the operation remains pending in the local outbox.
4. The service queues transcription; a committed transcript then queues summarization.
5. Completed derivative artifacts become independently readable and retain provenance to the source recording version.
6. Metadata edits create new revisions without rewriting immutable artifacts. The capture timestamp is not editable and
   is distinct from server receipt time and later metadata revision times.
7. Conversation deletion establishes a durable tombstone, cancels incompatible work, and removes all conversation data
   from every live store after the service has made deletion authoritative.
8. A non-sensitive deletion record survives conversation deletion so stale clients and workers cannot recreate any part
   of the deleted conversation.
9. The Android app retains each server-accepted recording locally for 30 days, then automatically removes the local
    source copy. The conversation remains browsable, and the app retrieves the recording from the server on demand when
    the source has not been deleted there.
10. Recordings are retained indefinitely in server storage unless the user deletes the entire conversation. Backup
    retention honors deletion policy so expired backup copies cannot silently become ordinary live recordings.

The mobile client retains its local recording until server integrity verification and acknowledgment. The 30-day local
retention window starts at server acceptance, and automatic cleanup is permitted only after that acknowledgment. If the
device is offline or synchronization is incomplete, the local recording remains available and its outbox operation stays
pending until a usable data connection allows synchronization to resume. A conversation deletion acknowledgment causes
every synchronized client to remove all local conversation data and discard stale upload operations.


### Sync protocol

Each client operation carries a stable operation identity, conversation identity, device identity, and the metadata
revision on which it is based. The recording operation also carries the device-captured timestamp, which the service
accepts once and rejects on later mutation attempts. The service records accepted operations so retries return the prior
result.
Client-created conversation identities allow metadata and recording upload to converge without duplicate server records.

The Android app initiates synchronization through a connectivity-aware background worker. Large recordings use a
resumable transfer coordinated by the API. The server grants only limited artifact access, records acknowledged parts,
and commits a recording only after whole-object size and digest verification. Abandoned transfers remain invisible and
are eventually reclaimed. If connectivity is lost during any sync operation, the Android worker preserves the outbox
operation and acknowledged upload progress, then resumes or re-attempts it when a usable data connection is available
again. Completion emits one logical processing request regardless of how often the client retries.

The outbox preserves operation order where causality matters, such as create before edit and upload before completion.
Independent conversations may synchronize concurrently. Temporary failures use bounded backoff but do not progress while
the device has no usable data connection. Connectivity restoration triggers another attempt. Authorization, conflict,
rejected media, and exhausted server capacity become visible attention states rather than infinite retry loops.

Metadata changes use optimistic concurrency. A stale update receives the current revision and values. The client keeps
the unsent intent and asks the user to retry or resolve differences. Lifecycle tombstones outrank stale creates,
uploads, and processing events. Server time orders committed revisions, while the immutable device-captured timestamp
records when the conversation was recorded.


### Agent integration

MCP is the initial agent integration because Muninn is a context and tool provider. A dedicated MCP adapter or gateway
translates MCP resources and tools into calls to the authenticated Muninn domain API. This matches OpenCode, Claude,
GitHub Copilot, and other MCP-capable hosts while keeping Muninn independent of any one agent runtime. The adapter
exposes bounded discovery, transcript, summary, and note retrieval plus the explicitly authorized add-agent-note tool.
It never gives an agent direct PostgreSQL, object-store, or worker access.

ACP is not an initial dependency. ACP addresses communication between editors, clients, and autonomous agents rather
than standardized access to Muninn's durable conversation data. It should be reconsidered only if Muninn later operates
its own autonomous agent that needs delegated tasks or agent-to-agent sessions. ACP does not replace MCP for context
access.

Agent search returns compact metadata, transcript and summary excerpts, and timestamped notes first. Full transcript or
summary retrieval requires selection of a specific authorized conversation, which limits accidental disclosure and
context-window waste. Agent-facing results carry conversation, artifact version, note author kind, and recording
timestamp metadata so generated work can cite its source. Agents may add notes through an explicit operation, and every
such note is marked as an agent note. Metadata mutation and conversation deletion remain unavailable to generic agent
sessions.


### Security

Keycloak is the identity provider for Android, web, CLI, TUI, and remote MCP access. The service validates credential
signature, issuer, audience, expiry, and required capabilities and derives ownership from an immutable identity subject.
Android and web clients use flows appropriate to public clients and do not embed reusable secrets. CLI and TUI clients
use Keycloak's device authorization grant. Local agent adapters use the user's delegated session rather than a shared
administrative identity.

Authorization applies at the conversation and operation boundaries. Storage and workers trust service-issued,
short-lived grants rather than end-user object credentials. Database, queue, object storage, and administrative
endpoints remain private to the homelab network. Transport is encrypted between clients and the homelab ingress and
wherever sensitive traffic crosses an untrusted boundary, including API calls to external AI providers. Provider
selection, credentials, model names, and request failures are configuration and operational metadata; provider responses
are stored only as versioned artifacts and are never written to routine telemetry.

Android protects queued audio, tokens, and metadata with platform-backed storage and minimizes lock-screen disclosure.
Server secrets come from the deployment's secret facility. Backups are encrypted and access-controlled. Audit events
record authentication outcomes, metadata and lifecycle changes, artifact grants, and deletion completion without
recording conversation content or credentials.

Deletion is a security property, not just a missing object reference. The service verifies removal of every conversation
artifact, retains a tombstone, prevents processing resurrection, and exposes deletion completion or failure. Backup
expiration and restore procedures preserve this intent. Recording consent remains the user's responsibility, while
persistent Android capture indicators prevent Muninn from acting as a covert recorder.


### Deployment

The homelab deployment consists of independently replaceable API, worker, PostgreSQL, MinIO-compatible storage, durable
queue, and MCP gateway workloads integrated with Keycloak. Helm charts package the Muninn workloads, configuration,
secret references, ingress, persistence, health checks, and resource policies for installation into the homelab
Kubernetes cluster. The charts target Tucker's microk8s environment. For local testing outside the intended deployment
context, DevSpace provides the development workflow and service orchestration. Development conveniences must not weaken
production identity or storage controls.

API and agent-access workloads scale independently from compute-heavy workers. Transcription and summarization can use
different compute pools, including accelerator-equipped nodes. Queue backpressure protects the API and database when AI
capacity is unavailable. Storage quotas and low-space thresholds prevent a burst of recordings from consuming the
homelab without warning.

PostgreSQL and object storage receive coordinated, encrypted backups with retention and restore verification. Keycloak
configuration and required identity mappings are recoverable as deployment state. Deployments support compatible schema
and worker transitions without making already captured recordings unreadable or processing the same source into
conflicting current artifacts.


### Observability

Structured logs correlate a user-safe conversation identifier, operation identity, upload session, processing attempt,
and request trace. Metrics cover request outcomes and latency, authentication failures, active and abandoned uploads,
outbox-visible service errors, queue depth and oldest age, stage duration and failure rate, artifact growth, storage
pressure, and deletion completion lag.

Health signals distinguish process liveness from readiness on PostgreSQL, object storage, identity discovery, and queue
connectivity. Worker status distinguishes unavailable capacity from bad input and provider failure. Alerts focus on
sustained backlog, repeated processing failure, inability to delete, backup failure, and capacity exhaustion.

Telemetry uses metadata allowlists. Audio, transcript and summary content, personal metadata values, credentials, and
signed locations are excluded. Operators can inspect lifecycle history for one conversation through identifiers and
state transitions without exposing its contents.


### Testing strategy

Domain contract tests cover ownership, revision conflicts, idempotency, lifecycle transitions, artifact manifests, and
tombstone precedence. API and client compatibility tests verify Android, CLI, TUI-ready service clients, and MCP clients
observe the same semantics. Security tests exercise invalid credentials, cross-owner access, expired artifact grants,
malicious metadata, and telemetry redaction.

Android tests cover capture state transitions, widget/app coordination, foreground-service behavior, permissions,
storage exhaustion, process death, restart, and queued operation recovery. Network fault tests interrupt every sync
phase, repeat acknowledgments, reorder safe operations, and verify eventual convergence without duplicate jobs.

Worker tests use representative short audio fixtures and deterministic substitute processors for routine automation.
Separate evaluation datasets measure transcription quality, speaker segmentation where enabled, and summary
faithfulness. Summaries must not invent decisions or action items; uncertain content remains attributed as uncertain.
Model or prompt changes require versioned regression evaluation before becoming current.

Deployment tests lint and install the Helm charts into a production-like Kubernetes environment, then exercise Keycloak,
PostgreSQL, object storage, queue, and worker integration. DevSpace tests verify the local development workflow outside
the intended deployment context. Recovery tests restore coordinated backups and verify tombstone behavior.
Manual release checks validate real-device recording, the widget, accessibility, long captures, poor networks, and
end-to-end context retrieval from each supported agent host.

----

## Unknowns

----

## Technical Notes

- Conversation, recording, transcript, and summary are separate lifecycle entities; a single status label cannot express
  all valid combinations.
- Zen API is the first-release external AI provider. The API dispatches through a provider adapter, keeps credentials in
  server-side configuration, records provider and model provenance, and treats provider failures as retryable or
  terminal processing states without exposing provider responses in telemetry.
- The first release supports English only.
- Homelab access is provided through a Cloudflare Tunnel. Cloudflare Tunnel configuration is supplied during the first
  deployment and remains deployment configuration rather than an application concern.
- Transcription labels distinct voices as `Speaker <N>`.
  Android and web can assign human-provided names to those labels.
  Automatic speaker identity recognition is out of scope.
- Generated artifacts are versioned because transcript corrections, processor changes, and summary regeneration must not
  destroy provenance.
- PostgreSQL stores queryable metadata and lifecycle history; S3-compatible storage holds immutable large content.
- The current scaffold's direct local-database CLI behavior is suitable only for development and must not become a
  second production authority.
- Summary quality is evaluated for source grounding, not fluency alone. Every generated claim should remain traceable to
  the selected transcript version.
