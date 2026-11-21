"""Expose bounded Streamable HTTP context access without storage credentials."""

import json
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from muninn.auth.dependencies import principal, require_capability
from muninn.auth.jwt import Principal
from muninn.db import db_session
from muninn.constants import AuthorKind, ArtifactKind, VisibilityState
from muninn.mcp.constants import (
    MAX_MCP_ARTIFACT_CHARACTERS,
    MAX_MCP_CONTEXT_BYTES,
    MAX_MCP_CONTEXT_NOTES,
    MAX_MCP_NOTE_CHARACTERS,
)
from muninn.db.models import ArtifactManifest, Conversation, Note
from muninn.db.idempotency import IdempotencyContext
from muninn.exceptions import ResourceNotFoundError
from muninn.mcp.exceptions import DelegatedSessionRequiredError, McpOffsetOutOfRangeError
from muninn.queue.exceptions import ObjectStoreNotFoundError
from muninn.storage import ObjectStore, get_object_store
from muninn.api.schemas import IdempotencyKeyHeader
from muninn.utils import request_fingerprint

router = APIRouter(prefix="/mcp", tags=["mcp"])


@dataclass(frozen=True, slots=True)
class McpCapabilityLimits:
    """Define the bounds applied to MCP context responses."""

    resource_characters: int = MAX_MCP_ARTIFACT_CHARACTERS
    notes: int = MAX_MCP_CONTEXT_NOTES
    total_bytes: int = MAX_MCP_CONTEXT_BYTES


@dataclass(frozen=True, slots=True)
class McpCapabilities:
    """Describe the bounded MCP resources and tools exposed by Muninn."""

    protocol: str
    resources: tuple[str, ...]
    tools: tuple[str, ...]
    limits: McpCapabilityLimits


@dataclass(frozen=True, slots=True)
class McpArtifactVersions:
    """Identify the current transcript and summary versions in a context response."""

    transcript: int | None
    summary: int | None


@dataclass(frozen=True, slots=True)
class McpContextNote:
    """Represent one bounded note in a context response."""

    conversation_id: str
    offset: float
    text: str
    author_kind: str
    artifact_version: str | None


@dataclass(slots=True)
class McpContext:
    """Represent the bounded context projection returned to an agent."""

    conversation_id: str
    artifact_versions: McpArtifactVersions
    transcript: str
    summary: str
    notes: list[McpContextNote]


class AgentNoteRequest(BaseModel):
    """Validate the sole first-release agent write operation."""

    conversation_id: UUID
    offset_seconds: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=20_000)

    @field_validator("offset_seconds")
    @classmethod
    def round_offset_to_milliseconds(cls, value: float) -> float:
        """Round note positions to the nearest millisecond."""
        if value < 0:
            raise ValueError("offset_seconds must be non-negative")
        return round(value * 1000) / 1000


def read_optional_artifact(store: ObjectStore, key: str) -> str:
    """Read an artifact, treating only an absent object as empty content."""
    try:
        data = store.get_bytes(key, limit=MAX_MCP_ARTIFACT_CHARACTERS)
        return data.decode("utf-8", errors="replace")
    except ObjectStoreNotFoundError:
        return ""


@router.get("")
def mcp_capabilities(caller: Principal = Depends(principal)) -> McpCapabilities:
    """Return bounded resources and the sole first-release tool."""
    require_capability("context:read", caller)
    return McpCapabilities(
        protocol="streamable-http",
        resources=("transcript", "summary", "notes"),
        tools=("add-agent-note",),
        limits=McpCapabilityLimits(),
    )


@router.get("/conversations/{conversation_id}")
def context(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> McpContext:
    """Return a bounded selected-conversation context projection."""
    require_capability("context:read", caller)

    item = (
        db.query(Conversation)
        .filter_by(id=conversation_id, owner_subject=caller.subject, visibility=VisibilityState.ACTIVE)
        .first()
    )
    item = ResourceNotFoundError.enforce_defined(item, "Conversation was not found")

    notes = (
        db.query(Note)
        .filter_by(conversation_id=conversation_id, deleted_at=None)
        .order_by(Note.offset_seconds)
        .limit(MAX_MCP_CONTEXT_NOTES)
        .all()
    )
    context_notes = [
        McpContextNote(
            conversation_id=str(note.conversation_id),
            offset=note.offset_seconds,
            text=note.text[:MAX_MCP_NOTE_CHARACTERS],
            author_kind=note.author_kind,
            artifact_version=note.artifact_version,
        )
        for note in notes
    ]

    manifests = db.query(ArtifactManifest).filter_by(conversation_id=conversation_id, is_current=True).all()
    manifests_by_kind = {manifest.kind: manifest for manifest in manifests}
    transcript_manifest = manifests_by_kind.get(ArtifactKind.TRANSCRIPT)
    summary_manifest = manifests_by_kind.get(ArtifactKind.SUMMARY)
    if summary_manifest is not None and (
        transcript_manifest is None
        or summary_manifest.source_version != transcript_manifest.source_version
        or summary_manifest.transcript_version != transcript_manifest.version
        or summary_manifest.speaker_revision != item.speaker_revision
    ):
        summary_manifest = None

    transcript = ""
    if transcript_manifest is not None:
        transcript = read_optional_artifact(store, transcript_manifest.object_key)

    summary = ""
    if summary_manifest is not None:
        summary = read_optional_artifact(store, summary_manifest.object_key)

    transcript_version = transcript_manifest.version if transcript_manifest is not None else None
    summary_version = summary_manifest.version if summary_manifest is not None else None
    result = McpContext(
        conversation_id=str(item.id),
        artifact_versions=McpArtifactVersions(
            transcript=transcript_version,
            summary=summary_version,
        ),
        transcript=transcript[:MAX_MCP_ARTIFACT_CHARACTERS],
        summary=summary[:MAX_MCP_ARTIFACT_CHARACTERS],
        notes=context_notes,
    )
    while len(json.dumps(jsonable_encoder(result)).encode()) > MAX_MCP_CONTEXT_BYTES and result.notes:
        result.notes.pop()
    return result


@router.post("/tools/add-agent-note", status_code=201)
def add_agent_note(
    payload: AgentNoteRequest,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    session_id: UUID | None = Header(default=None, alias="X-Muninn-Agent-Session"),
    db: Session = Depends(db_session),
) -> dict[str, object]:
    """Create an immutable agent note only when explicit delegated capability exists."""
    require_capability("notes:agent:create", caller)
    fingerprint = request_fingerprint({"conversation_id": str(payload.conversation_id), "offset_seconds": payload.offset_seconds, "text": payload.text, "session_id": str(session_id)})
    item = db.query(Conversation).filter_by(id=payload.conversation_id, owner_subject=caller.subject).first()
    item = ResourceNotFoundError.enforce_defined(item, "Conversation was not found")
    McpOffsetOutOfRangeError.require_condition(
        item.recording_duration_seconds is None or payload.offset_seconds <= item.recording_duration_seconds,
        "Note offset is outside the recording",
    )
    session_id = DelegatedSessionRequiredError.ensure_type(session_id, UUID, "A delegated session is required")
    with IdempotencyContext(db, caller.subject, "agent_note", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            return idempotency.response_data or {"id": str(idempotency.resource_id)}
        note = Note(
            conversation_id=item.id,
            subject=caller.subject,
            author_kind=AuthorKind.AGENT,
            delegated_session_id=session_id,
            text=payload.text,
            offset_seconds=payload.offset_seconds,
        )
        db.add(note)
        db.flush()
        response = {
            "id": str(note.id),
            "conversation_id": str(item.id),
            "author_kind": AuthorKind.AGENT.value,
            "offset": payload.offset_seconds,
        }
        idempotency.capture(note.id, response)
    db.commit()
    return response
