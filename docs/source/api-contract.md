# Muninn API contract

This contract describes the stable authentication, lifecycle, upload,
note, and service-boundary behavior of the first release.


## Service boundary

Clients and MCP call the authenticated API. Only API/domain services write PostgreSQL and only service code accesses
MinIO or NATS JetStream. PostgreSQL is authoritative for metadata, manifests,
jobs, tombstones, and revisions. Object keys are never authority.


## Authentication matrix

| Client  | Flow                              | Client ID        | Redirect                                              | Audience     |
| ------- | --------------------------------- | ---------------- | ----------------------------------------------------- | ------------ |
| Android | Authorization code with S256 PKCE | `muninn-android` | `com.muninn.app:/oauth2redirect`                      | `muninn-api` |
| Web     | Authorization code with S256 PKCE | `muninn-web`     | `https://${global.publicHostname}/auth/callback`      | `muninn-api` |
| MCP     | Delegated user session            | `muninn-mcp`     | `https://${global.publicHostname}/mcp/oauth/callback` | `muninn-api` |

`global.publicHostname` is a lowercase FQDN without a scheme, port, path, query, fragment, wildcard, or trailing dot.
The service derives the HTTPS public origin from that hostname. Access tokens and artifact grants last 600 seconds.
Delegated sessions expire after 900 seconds of inactivity and 28,800 seconds maximum.

| Failure              | Kind                        | HTTP |
| -------------------- | --------------------------- | ---- |
| Missing bearer       | `authentication_required`   | 401  |
| Invalid credential   | `credential_invalid`        | 401  |
| Expired credential   | `credential_expired`        | 401  |
| Not active           | `credential_not_active`     | 401  |
| Wrong issuer         | `credential_wrong_issuer`   | 401  |
| Wrong audience       | `credential_wrong_audience` | 401  |
| Revoked credential   | `credential_revoked`        | 401  |
| Missing capability   | `capability_denied`         | 403  |
| Cross-owner resource | `resource_not_found`        | 404  |

All errors use `{ "error": { "kind": "...", "message": "...", "request_id":
"uuid" } }` and 401 responses include `WWW-Authenticate: Bearer`.


## Lifecycle and upload

Capture time is immutable. Metadata updates use a revision precondition and return current values on conflict. Deletion
commits a tombstone identified by conversation ID and deletion time, removes governed rows, and best-effort cleans
object storage. Stale uploads, jobs, MCP calls, and retries cannot resurrect
data. The API returns `conversation_deleted` after the tombstone commit.

Uploads use stable conversation and operation IDs, resumable acknowledged parts, exact byte and SHA-256 checks, and a
single committed manifest. Admission limits are 512 MiB, three hours, ten active uploads, 10 GiB committed source quota,
and 20 starts per owner per hour. Allowed media is WAV, M4A/AAC, or WebM/Opus.
Grants are operation-specific and expire after ten minutes.

The owner can retrieve the current normalized English transcript with `GET
/v1/conversations/{conversation_id}/transcript`. The response contains timestamped speaker segments. The endpoint
requires `context:read` and returns `resource_not_found` until a transcript is committed.

The owner can retrieve the current structured summary with `GET /v1/conversations/{conversation_id}/summary`. The
endpoint requires `context:read` and returns `resource_not_found` until a summary is committed.

The owner can list durable processing jobs with `GET /v1/conversations/{conversation_id}/jobs`. The endpoint returns job
kind, state, attempts, and creation time. The web UI presents queued jobs as `STAGED`, running jobs as `RUNNING`,
successful jobs as `COMPLETED`, and failed jobs as `FAILED`. It shows an indeterminate progress bar while jobs run;
AssemblyAI does not provide percentage completion for pre-recorded transcription.

The web client automatically stages transcription for uploaded recordings with no current job using `POST
/v1/conversations/{conversation_id}/jobs/transcription`. The endpoint requires `processing:admin` and
an idempotency key; submitting a new key after a failed job creates a retry job.


## Notes and MCP

Notes contain conversation ID, non-negative `offset_seconds` values rounded to the nearest millisecond, text, author
identity, author kind, creation time, and artifact version where relevant. Offsets cannot exceed known duration. Human
owners can create and delete their notes, and edit note text through `PATCH
/v1/conversations/{conversation_id}/notes/{note_id}` with `notes:human:edit`. Edits preserve the note timestamp and
creation time. Agent notes are immutable in the first release and require `notes:agent:create`.

MCP is Streamable HTTP at `/mcp`. Search returns at most 20 rows and 2,000 characters per excerpt. Selected resources
return at most 12,000 characters each, 50 notes, and 64 KiB total. Opaque cursors expire
after 15 minutes and are owner-scoped. The only MCP tool is `add-agent-note`.
