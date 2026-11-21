"""SQLAlchemy persistence models for the Muninn control plane."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import inflection
from sqlalchemy import DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column, relationship

from muninn.api.constants import OperationKind, OperationStatus, UploadState
from muninn.constants import (
    AuthorKind,
    ArtifactKind,
    ProcessingErrorCode,
    ProcessingKind,
    ProcessingState,
    SyncState,
    VisibilityState,
)


class Base(DeclarativeBase):
    """Provide typed SQLAlchemy metadata."""

    @declared_attr.directive
    def __tablename__(cls) -> str:
        """Derive each mapped table name from its model class name."""
        return inflection.tableize(cls.__name__)


class IdempotencyRecord(Base):
    """Store caller-scoped mutation identity independently of managed resources."""

    __table_args__ = (
        UniqueConstraint("owner_subject", "operation_kind", "idempotency_key"),
        Index("ix_idempotency_records_resource_id", "resource_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_subject: Mapped[str] = mapped_column(Text(), nullable=False)
    operation_kind: Mapped[str] = mapped_column(Text(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(Text(), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column()
    response_data: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(Text(), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Conversation(Base):
    """Store conversation metadata and lifecycle projections."""

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    owner_subject: Mapped[str] = mapped_column(Text(), index=True)
    name: Mapped[str] = mapped_column(Text(), default="Untitled conversation")
    description: Mapped[str | None] = mapped_column(Text())
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    people: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(default=1)
    speaker_revision: Mapped[int] = mapped_column(default=0)
    sync_state: Mapped[SyncState] = mapped_column(
        Enum(SyncState, name="sync_state", values_callable=lambda enum_type: [member.value for member in enum_type]),
        default=SyncState.LOCAL_ONLY,
    )
    visibility: Mapped[VisibilityState] = mapped_column(
        Enum(VisibilityState, name="visibility_state", values_callable=lambda enum_type: [member.value for member in enum_type]),
        default=VisibilityState.ACTIVE,
    )
    recording_duration_seconds: Mapped[float | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    notes: Mapped[list["Note"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    speakers: Mapped[list["ConversationSpeaker"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class Note(Base):
    """Store a timestamped human or delegated-agent note."""

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(Text())
    author_kind: Mapped[AuthorKind] = mapped_column(
        Enum(
            AuthorKind,
            name="author_kind",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        ),
        default=AuthorKind.HUMAN,
    )
    delegated_session_id: Mapped[UUID | None] = mapped_column()
    offset_seconds: Mapped[float] = mapped_column(Float())
    text: Mapped[str] = mapped_column(Text())
    artifact_version: Mapped[str | None] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    conversation: Mapped[Conversation] = relationship(back_populates="notes")


class Tombstone(Base):
    """Retain a non-sensitive deletion marker that prevents resurrection."""

    conversation_id: Mapped[UUID] = mapped_column(primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Operation(Base):
    """Record an idempotent client operation and its current outcome."""

    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False)
    kind: Mapped[OperationKind] = mapped_column(
        Enum(
            OperationKind,
            name="operation_kind",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        )
    )
    status: Mapped[OperationStatus] = mapped_column(
        Enum(
            OperationStatus,
            name="operation_status",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        ),
        default=OperationStatus.ACCEPTED,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class UploadSession(Base):
    """Track resumable upload admission without making partial objects readable."""

    __table_args__ = (UniqueConstraint("conversation_id", "operation_id"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(ForeignKey("operations.id"), unique=True, nullable=False)
    expected_size: Mapped[int] = mapped_column(nullable=False)
    expected_sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    acknowledged_bytes: Mapped[int] = mapped_column(default=0, nullable=False)
    state: Mapped[UploadState] = mapped_column(
        Enum(
            UploadState,
            name="upload_state",
            values_callable=lambda enum_type: [member.value for member in enum_type],
            validate_strings=True,
        ),
        default=UploadState.UPLOADING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ArtifactManifest(Base):
    """Describe an immutable object-store artifact and its verified digest."""

    __table_args__ = (
        UniqueConstraint("conversation_id", "kind", "version"),
        Index(
            "uq_artifact_manifests_current",
            "conversation_id",
            "kind",
            unique=True,
            postgresql_where=text("is_current"),
            sqlite_where=text("is_current"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=False)
    kind: Mapped[ArtifactKind] = mapped_column(
        Enum(ArtifactKind, name="artifact_kind", values_callable=lambda enum_type: [member.value for member in enum_type]),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    object_key: Mapped[str] = mapped_column(Text(), unique=True, nullable=False)
    sha256: Mapped[str] = mapped_column(Text(), nullable=False)
    byte_size: Mapped[int] = mapped_column(nullable=False)
    source_version: Mapped[int] = mapped_column(nullable=False)
    transcript_version: Mapped[int | None] = mapped_column()
    speaker_revision: Mapped[int] = mapped_column(default=0, nullable=False)
    is_current: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ProcessingAttempt(Base):
    """Record one bounded processing attempt for a transcription or summary job."""

    __table_args__ = (UniqueConstraint("job_id", "attempt_number"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True, nullable=False)
    job_id: Mapped[UUID] = mapped_column(index=True, nullable=False)
    kind: Mapped[ProcessingKind] = mapped_column(
        Enum(ProcessingKind, name="processing_kind", values_callable=lambda enum_type: [member.value for member in enum_type]),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(nullable=False)
    state: Mapped[ProcessingState] = mapped_column(
        Enum(ProcessingState, name="processing_state", values_callable=lambda enum_type: [member.value for member in enum_type]),
        default=ProcessingState.RUNNING,
        nullable=False,
    )
    error_code: Mapped[ProcessingErrorCode | None] = mapped_column(
        Enum(
            ProcessingErrorCode,
            name="processing_error_code",
            native_enum=False,
            values_callable=lambda enum_type: [member.value for member in enum_type],
            length=64,
        )
    )
    error_details: Mapped[str | None] = mapped_column(Text())
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Speaker(Base):
    """Store a reusable known-speaker identity."""

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4, nullable=False)
    name: Mapped[str] = mapped_column(Text(), nullable=False)


class ConversationSpeaker(Base):
    """Associate a transcript-local speaker identity with an optional catalog speaker."""

    __table_args__ = (
        UniqueConstraint("conversation_id", "speaker_number"),
        Index(
            "uq_conversation_speakers_known_speaker",
            "conversation_id",
            "speaker_id",
            unique=True,
            postgresql_where=text("speaker_id IS NOT NULL"),
            sqlite_where=text("speaker_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4, nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    speaker_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("speakers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    speaker_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text(), nullable=True)

    conversation: Mapped[Conversation] = relationship(back_populates="speakers")
    speaker: Mapped[Speaker | None] = relationship()


class AuditEvent(Base):
    """Store non-content operational facts needed to explain lifecycle changes."""

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    operation_id: Mapped[UUID | None] = mapped_column(ForeignKey("operations.id"), index=True)
    actor: Mapped[str] = mapped_column(Text())
    event_type: Mapped[str] = mapped_column(Text())
    reason: Mapped[str] = mapped_column(Text())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class OutboxMessage(Base):
    """Persist a job publication until the single job manager acknowledges it."""

    __table_args__ = (UniqueConstraint("deduplication_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    subject: Mapped[str] = mapped_column(Text())
    payload: Mapped[dict] = mapped_column(JSON)
    deduplication_key: Mapped[str] = mapped_column(Text())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(default=0)


class Job(Base):
    """Represent a durable processing job reconstructed by the job manager."""

    __table_args__ = (
        Index(
            "uq_jobs_active_transcription",
            "conversation_id",
            "source_version",
            unique=True,
            postgresql_where=text("kind = 'transcription' AND state IN ('queued', 'running')"),
            sqlite_where=text("kind = 'transcription' AND state IN ('queued', 'running')"),
        ),
        Index(
            "uq_jobs_active_summarization",
            "conversation_id",
            "source_version",
            "transcript_version",
            "speaker_revision",
            unique=True,
            postgresql_where=text("kind = 'summarization' AND state IN ('queued', 'running')"),
            sqlite_where=text("kind = 'summarization' AND state IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    kind: Mapped[ProcessingKind] = mapped_column(
        Enum(ProcessingKind, name="processing_kind", values_callable=lambda enum_type: [member.value for member in enum_type])
    )
    source_version: Mapped[int] = mapped_column(default=1)
    transcript_version: Mapped[int | None] = mapped_column()
    speaker_revision: Mapped[int] = mapped_column(default=0, nullable=False)
    speaker_mapping: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[ProcessingState] = mapped_column(
        Enum(ProcessingState, name="processing_state", values_callable=lambda enum_type: [member.value for member in enum_type]),
        default=ProcessingState.QUEUED,
    )
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
