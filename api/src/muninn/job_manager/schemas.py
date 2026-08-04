"""Data and protocol schemas used by the job manager."""

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol, Self
from uuid import UUID

import snick
from sqlalchemy.orm import Session

from muninn.constants import ArtifactKind
from muninn.db.models import ArtifactManifest, Conversation, Job


@dataclass(frozen=True)
class CompletionRejectionContext:
    """Carry safe identifiers and correlation values for a rejected completion."""

    job_id: UUID
    conversation_id: UUID
    event_kind: str | None = None
    job_kind: str | None = None
    event_source_version: int | None = None
    job_source_version: int | None = None
    event_transcript_version: int | None = None
    job_transcript_version: int | None = None

    def with_job(self, job_kind: str, source_version: int, transcript_version: int | None) -> Self:
        """Return context augmented with values read from the current job row."""
        return type(self)(
            self.job_id, self.conversation_id, self.event_kind, job_kind,
            self.event_source_version, source_version, self.event_transcript_version, transcript_version,
        )

    def describe(self) -> str:
        """Render stable completion context without artifact data."""
        return snick.conjoin(
            f"job_id={self.job_id}", f"conversation_id={self.conversation_id}", f"event_kind={self.event_kind}",
            f"job_kind={self.job_kind}", f"event_source_version={self.event_source_version}",
            f"job_source_version={self.job_source_version}", f"event_transcript_version={self.event_transcript_version}",
            f"job_transcript_version={self.job_transcript_version}", join_str=", ",
        )


class SessionFactory(Protocol):
    """Create a database session context with optional transaction handling."""

    def __call__(self, in_transaction: bool = False) -> AbstractContextManager[Session]:
        """Return a context manager for one database session."""


@dataclass(frozen=True, slots=True)
class ValidatedCompletion:
    """Hold the conversation and job rows locked for one completion transaction."""

    conversation: Conversation
    job: Job


@dataclass(frozen=True, slots=True)
class ManifestVersion:
    """Hold the artifact type and manifest versions for one completion transaction."""

    kind: ArtifactKind
    current: ArtifactManifest | None
    next_version: int
