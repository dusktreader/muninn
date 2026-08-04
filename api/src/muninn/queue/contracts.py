"""Define versioned queue message value objects."""

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from buzz import require_condition
from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator

from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState


class ResultObject(BaseModel):
    """Represent a verified object reference."""

    model_config = {"extra": "forbid"}

    kind: ArtifactKind
    bucket: str
    key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)


class OutboxJobPayload(BaseModel):
    """Represent the validated JSON payload persisted for an outbox job."""

    model_config = {"extra": "forbid"}

    job_id: UUID
    conversation_id: UUID
    kind: ProcessingKind
    source_version: StrictInt = Field(ge=1)
    transcript_version: StrictInt | None = Field(default=None, ge=1)
    speaker_revision: StrictInt = Field(default=0, ge=0)
    speaker_mapping: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    source_key: StrictStr = Field(min_length=1, max_length=1024)


class JobMessage(BaseModel):
    """Represent one versioned request to process a conversation artifact."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = 1
    job_id: UUID
    conversation_id: UUID
    kind: ProcessingKind
    source_version: int = Field(ge=1)
    transcript_version: int | None = Field(default=None, ge=1)
    speaker_revision: int = Field(default=0, ge=0)
    speaker_mapping: dict[str, str] = Field(default_factory=dict)
    source_object: ResultObject | None = None
    attempt: int = Field(default=1, ge=1)


class CompletionEvent(BaseModel):
    """Represent the outcome of processing a conversation artifact."""

    model_config = {"extra": "forbid"}

    schema_version: Literal[1] = 1
    job_id: UUID
    conversation_id: UUID
    kind: ProcessingKind
    source_version: int = Field(ge=1)
    transcript_version: int | None = Field(default=None, ge=1)
    speaker_revision: int = Field(default=0, ge=0)
    state: ProcessingState
    result_object: ResultObject | None = None
    error_code: ProcessingErrorCode | None = None
    error_details: str | None = None
    retryable: bool = False
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_payload(self) -> "CompletionEvent":
        """Require the payload appropriate to a successful or failed event."""
        require_condition(
            self.state is not ProcessingState.SUCCEEDED or self.result_object is not None,
            "successful completion requires a result object",
            raise_exc_class=ValueError,
        )
        require_condition(
            self.state is not ProcessingState.FAILED or self.error_code is not None,
            "failed completion requires an error code",
            raise_exc_class=ValueError,
        )
        require_condition(
            self.state is not ProcessingState.FAILED or self.result_object is None,
            "failed completion must not contain a result object",
            raise_exc_class=ValueError,
        )
        require_condition(
            self.state is not ProcessingState.SUCCEEDED or self.error_code is None,
            "successful completion must not contain an error code",
            raise_exc_class=ValueError,
        )
        require_condition(
            self.state is not ProcessingState.SUCCEEDED or self.error_details is None,
            "successful completion must not contain error details",
            raise_exc_class=ValueError,
        )
        require_condition(
            self.state is not ProcessingState.RUNNING
            or (self.result_object is None and self.error_code is None and self.error_details is None),
            "running event must not contain completion data",
            raise_exc_class=ValueError,
        )
        if self.result_object is not None:
            expected_kind = ArtifactKind.TRANSCRIPT if self.kind is ProcessingKind.TRANSCRIPTION else ArtifactKind.SUMMARY
            require_condition(
                self.result_object.kind is expected_kind,
                "completion result artifact type does not match processing kind",
                raise_exc_class=ValueError,
            )
        return self
