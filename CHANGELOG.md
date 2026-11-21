# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## Unreleased

### Added

- Add GitHub Actions workflows for QA, documentation deployment, and tagged
  releases.
- Add idempotent NATS JetStream and Object Store provisioning during Helm
  deployments.

### Changed

- Update the API and documentation toolchains to Python 3.15 and refresh locked
  dependencies.
- Simplify Helm deployment around chart defaults, removing environment-specific
  values profiles.
- Add repository-wide dependency synchronization targets.

### Fixed

- Handle optional artifact grant secrets safely and preserve SQLite playback
  lock row counts.

## v0.1.0 - 2026-11-20

### Added

- Add a FastAPI service with Keycloak authentication, PostgreSQL metadata,
  MinIO artifact storage, NATS-backed processing, and MCP integration.
- Add Helm deployment configuration, local Keycloak setup, and backup and
  restore scripts for homelab operations.
- Add a React web UI for recording, conversation review, transcript and summary
  processing, and OIDC sign-in.
- Add an initial Android capture app scaffold with foreground recording, a
  home-screen widget, offline storage, and sync work.
- Add component-specific documentation, acceptance and release contracts, and
  automated API, web, and mobile tests.

### Changed

- Restructure the repository as a monorepo with separate API, web UI, mobile
  UI, documentation, and infrastructure components.
- Replace the original API package layout with the new API service and worker architecture.
