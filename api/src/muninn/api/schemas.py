"""Public API boundary schemas."""

from datetime import datetime
from typing import Annotated
from unicodedata import category, normalize
from uuid import UUID

from fastapi import Header
from pydantic import BaseModel, Field, field_validator

from muninn.constants import AuthorKind, ErrorKind, ProcessingErrorCode, ProcessingKind, ProcessingState, SyncState, VisibilityState
from muninn.schemas import IdempotencyKey


IdempotencyKeyHeader = Annotated[IdempotencyKey, Header(..., alias="X-Idempotency-Key")]
"""Represent the UUID supplied in the HTTP `X-Idempotency-Key` header."""


class ConversationCreate(BaseModel):
    """Validate a client-created conversation."""

    id: UUID | None = None
    name: str = "Untitled conversation"
    description: str | None = None
    occurred_at: datetime
    people: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    """Validate mutable metadata without accepting lifecycle fields."""

    name: str | None = None
    description: str | None = None
    people: list[str] | None = None
    tags: list[str] | None = None
    expected_revision: int = Field(default=1, ge=1)


class ConversationResponse(BaseModel):
    """Represent a conversation without storage credentials."""

    model_config = {"from_attributes": True}

    id: UUID
    name: str
    description: str | None
    occurred_at: datetime
    people: list[str]
    tags: list[str]
    revision: int
    sync_state: SyncState
    visibility: VisibilityState
    recording_available: bool
    transcript_available: bool
    summary_available: bool


class ProcessingJobResponse(BaseModel):
    """Represent one processing job without provider or storage internals."""

    model_config = {"from_attributes": True}

    id: UUID
    kind: ProcessingKind
    state: ProcessingState
    attempts: int
    created_at: datetime
    last_error_code: ProcessingErrorCode | None = None
    last_error_details: str | None = None


class SpeakerRenameRequest(BaseModel):
    """Validate a revision-checked transcript speaker rename."""

    model_config = {"extra": "forbid"}

    expected_speaker_revision: int = Field(ge=0)
    speaker_number: int = Field(ge=1)
    display_name: str | None = Field(..., max_length=100)

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        """Normalize a speaker name or reject blank and control-character values."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("display_name must be a string or null")
        normalized = " ".join(normalize("NFKC", value).split())
        if not normalized:
            raise ValueError("display_name must not be blank")
        if any(category(character) in {"Cc", "Cf", "Cs"} for character in normalized):
            raise ValueError("display_name must not contain control characters")
        return normalized


class ConversationSpeakerResponse(BaseModel):
    """Represent one transcript-local speaker label and its optional custom name."""

    speaker_number: int
    label: str
    display_name: str | None


class SpeakerMappingResponse(BaseModel):
    """Represent the current speaker mapping and an optional summary job."""

    speaker_revision: int
    speakers: list[ConversationSpeakerResponse]
    summary_job: ProcessingJobResponse | None = None


class NoteCreate(BaseModel):
    """Validate a timestamped note."""

    offset_seconds: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("offset_seconds")
    @classmethod
    def round_offset_to_milliseconds(cls, value: float) -> float:
        """Round note positions to the nearest millisecond."""
        if value < 0:
            raise ValueError("offset_seconds must be non-negative")
        return round(value * 1000) / 1000

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject whitespace-only note content."""
        if not value.strip():
            raise ValueError("text must not be blank")
        return value

class NoteUpdate(BaseModel):
    """Validate a human edit to note text without allowing timestamp changes."""

    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        """Reject whitespace-only note content."""
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class NoteResponse(BaseModel):
    """Represent a note with its immutable authorship fields."""

    model_config = {"from_attributes": True}

    id: UUID
    conversation_id: UUID
    offset_seconds: float
    text: str
    subject: str
    author_kind: AuthorKind
    delegated_session_id: UUID | None
    created_at: datetime


class ErrorDetail(BaseModel):
    """Represent the stable error payload returned to clients."""

    kind: ErrorKind
    message: str
    request_id: UUID


class ErrorResponse(BaseModel):
    """Represent the top-level API error envelope."""

    error: ErrorDetail
