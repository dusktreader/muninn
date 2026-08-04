# Muninn release contract

This document defines the first-release boundaries for the authenticated Muninn service and its capture clients.


## Client behavior

Android and web are capture clients. Android alone supports offline capture, encrypted local audio caching, and deferred
resumable synchronization. The browser requires connectivity for upload and keeps audio only in memory; it never writes
audio to IndexedDB, Cache Storage, service-worker storage, or application files.

No client writes PostgreSQL, MinIO, or NATS directly. MCP uses a delegated session,
exposes bounded reads, and has one write operation: `add-agent-note`.


## Processing and data policy

AssemblyAI processes audio server-side, English-only, and labels voices as `Speaker <N>`. Its API key stays server-side.
Human and agent notes are timestamped. Agent notes require `notes:agent:create`
and cannot edit or delete notes, metadata, lifecycle, or deletion state.


## Operations

PostgreSQL 18 stores metadata and lifecycle state, MinIO stores immutable artifacts, and NATS JetStream delivers durable
jobs from its dedicated PVC. Helm packages the homelab deployment, DevSpace supplies local development, and the homelab
project supplies public ingress without Cloudflare resources in this repository.


## Deletion

Whole-conversation deletion is the only removal path. There is no archive feature or archive state. The API is
authoritative after acknowledgement. Android's encrypted outbox is authoritative only for unacknowledged local work.
