"""Shared schemas used at service boundaries."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


IdempotencyKey = UUID
"""Represent a UUID used to identify an idempotent mutation."""


class TranscriptSegment(BaseModel):
    """Represent one English transcript segment with a stable evidence ID."""

    evidence_id: str = Field(min_length=1)
    speaker: str = Field(pattern=r"^Speaker [0-9]+$")
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1)

    @field_validator("evidence_id")
    @classmethod
    def validate_evidence_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("evidence_id must not be empty")
        return value

    @field_validator("end_seconds")
    @classmethod
    def validate_end_time(cls, value: float, info) -> float:
        """Reject a segment whose end precedes its start."""
        if value < info.data.get("start_seconds", value):
            raise ValueError("end_seconds must not precede start_seconds")
        return value


class TranscriptResult(BaseModel):
    """Represent normalized transcription output."""

    language: Literal["en"] = "en"
    segments: list[TranscriptSegment]
    provider: str
    model: str


class SummaryItem(BaseModel):
    """Represent one summary item with source confidence."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    evidence_span_ids: list[str] = Field(min_length=1)
    confidence: Literal["certain", "uncertain"] = "certain"
    owner: str | None = None
    due_date: str | None = None

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary text must not be empty")
        return value

    @field_validator("evidence_span_ids")
    @classmethod
    def validate_evidence_span_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("evidence IDs must not be empty")
        return values


class SummaryResult(BaseModel):
    """Represent the normalized summary shape."""

    model_config = ConfigDict(extra="forbid")

    overview: SummaryItem | None = None
    key_topics: list[SummaryItem] = Field(default_factory=list)
    decisions: list[SummaryItem] = Field(default_factory=list)
    action_items: list[SummaryItem] = Field(default_factory=list)
    provider: str
    model: str


def validate_summary_evidence(summary: SummaryResult, transcript: TranscriptResult) -> None:
    valid_evidence_ids = {segment.evidence_id for segment in transcript.segments}
    items = [summary.overview, *summary.key_topics, *summary.decisions, *summary.action_items]
    if any(
        evidence_id not in valid_evidence_ids
        for item in items
        if item is not None
        for evidence_id in item.evidence_span_ids
    ):
        raise ValueError("summary referenced evidence missing from transcript")
