"""Test focused conversation API contracts against isolated persistence."""

from collections.abc import Iterator
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from typing import BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from botocore.response import StreamingBody
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from muninn.api import app
from muninn.api.exceptions import RecordingPlaybackError
from muninn.api.routes.conversations import (
    PLAYBACK_DERIVATIVE_ADVISORY_LOCK_ID,
    acquire_playback_build_lock,
    recording_download_filename,
)
from muninn.auth.dependencies import principal, recording_principal
from muninn.auth.jwt import Principal
from muninn.config import service_settings
from muninn.api.constants import UploadState
from muninn.constants import ArtifactKind, AuthorKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.db import db_session
from muninn.mcp.routes import AgentNoteRequest, context as mcp_context
from muninn.db.models import ArtifactManifest, Base, Conversation, Job, Note, OutboxMessage, ProcessingAttempt
from muninn.api.schemas import NoteCreate, NoteResponse, NoteUpdate
from muninn.schemas import SummaryResult, TranscriptResult
from muninn.queue.exceptions import ObjectStoreNotFoundError
from muninn.storage import ObjectStore, get_object_store


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Provide an API client with an isolated in-memory database and owner."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def override_session() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_session] = override_session
    test_principal = Principal(
        "owner-1",
        frozenset({"context:read", "metadata:write", "deletion:write", "processing:admin", "notes:human:create", "notes:human:edit"}),
    )
    app.dependency_overrides[principal] = lambda: test_principal
    app.dependency_overrides[recording_principal] = lambda: test_principal
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_conversation(client: TestClient) -> dict[str, object]:
    """Create one conversation and return its public projection."""
    response = client.post(
        "/v1/conversations",
        json={"name": "Contract test", "occurred_at": datetime.now(timezone.utc).isoformat()},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.unit
def test_stage_transcription_is_idempotent_and_allows_reprocessing_failed_jobs(client: TestClient) -> None:
    """Return the staged job on repeat requests and create a retry after terminal failure."""
    conversation = create_conversation(client)
    payload = b"recording"
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add(
        ArtifactManifest(
            conversation_id=UUID(str(conversation["id"])),
            kind=ArtifactKind.RECORDING,
            object_key="conversations/recording.wav",
            sha256=sha256(payload).hexdigest(),
            byte_size=len(payload),
            source_version=1,
            is_current=True,
        )
    )
    session.commit()
    sessions.close()

    path = f"/v1/conversations/{conversation['id']}/jobs/transcription"
    stage_key = str(uuid4())
    stage_headers = {"X-Idempotency-Key": stage_key}
    first = client.post(path, headers=stage_headers)
    repeated = client.post(path, headers=stage_headers)

    assert first.status_code == 202
    assert repeated.status_code == 202
    assert first.json()["id"] == repeated.json()["id"]
    assert first.json()["state"] == "queued"

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    failed_job = session.get(Job, UUID(first.json()["id"]))
    failed_job.state = ProcessingState.FAILED
    session.commit()
    sessions.close()

    retried = client.post(path, headers={"X-Idempotency-Key": str(uuid4())})
    assert retried.status_code == 202
    assert retried.json()["id"] != first.json()["id"]
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    assert session.query(OutboxMessage).count() == 2
    sessions.close()


@pytest.mark.unit
def test_get_transcript_returns_the_current_owned_artifact(client: TestClient) -> None:
    """Return the normalized current transcript through the owner-authorized API."""
    conversation = create_conversation(client)
    transcript = TranscriptResult.model_validate(
        {
            "segments": [
                {
                    "evidence_id": "segment-1",
                    "speaker": "Speaker 1",
                    "start_seconds": 0.5,
                    "end_seconds": 1.5,
                    "text": "Hello from the transcript.",
                }
            ],
            "provider": "assemblyai",
            "model": "universal-3-5-pro",
        }
    )
    payload = transcript.model_dump_json().encode()
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add(
        ArtifactManifest(
            conversation_id=UUID(str(conversation["id"])),
            kind=ArtifactKind.TRANSCRIPT,
            object_key="conversations/transcript.json",
            sha256=sha256(payload).hexdigest(),
            byte_size=len(payload),
            source_version=1,
            is_current=True,
        )
    )
    session.commit()
    sessions.close()

    class FakeStore:
        def get_bytes(self, key: str) -> bytes:
            assert key == "conversations/transcript.json"
            return payload

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    response = client.get(f"/v1/conversations/{conversation['id']}/transcript")

    assert response.status_code == 200
    assert response.json()["segments"][0]["text"] == "Hello from the transcript."
    assert response.json()["segments"][0]["speaker"] == "Speaker 1"


@pytest.mark.unit
def test_speaker_rename_is_revisioned_idempotent_and_conversation_scoped(client: TestClient) -> None:
    """Persist one normalized speaker name with a mapped summary job and atomic outbox row."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    transcript = TranscriptResult(
        segments=[
            {
                "evidence_id": "segment-1",
                "speaker": "Speaker 1",
                "start_seconds": 0,
                "end_seconds": 1,
                "text": "A speaker said hello.",
            },
            {
                "evidence_id": "segment-2",
                "speaker": "Speaker 2",
                "start_seconds": 1,
                "end_seconds": 2,
                "text": "Another speaker replied.",
            },
        ],
        provider="assemblyai",
        model="universal-3-5-pro",
    )
    transcript_bytes = transcript.model_dump_json().encode()
    summary_bytes = SummaryResult(
        overview=None,
        key_topics=[],
        decisions=[],
        action_items=[],
        provider="assemblyai",
        model="gemma-4-31b",
    ).model_dump_json().encode()
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    stored_conversation = session.get(Conversation, conversation_id)
    stored_conversation.people = ["Existing person"]
    session.add_all(
        [
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=1,
                object_key="conversations/speakers-transcript.json",
                sha256=sha256(transcript_bytes).hexdigest(),
                byte_size=len(transcript_bytes),
                source_version=1,
                is_current=True,
            ),
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="conversations/speakers-summary.json",
                sha256=sha256(summary_bytes).hexdigest(),
                byte_size=len(summary_bytes),
                source_version=1,
                transcript_version=1,
                is_current=True,
            ),
            Note(
                conversation_id=conversation_id,
                subject="owner-1",
                author_kind=AuthorKind.HUMAN,
                offset_seconds=0.5,
                text="Keep this note.",
            ),
        ]
    )
    session.commit()
    sessions.close()

    class FakeStore:
        def get_bytes(self, key: str) -> bytes:
            if key == "conversations/speakers-transcript.json":
                return transcript_bytes
            if key == "conversations/speakers-summary.json":
                return summary_bytes
            raise AssertionError(key)

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    path = f"/v1/conversations/{conversation_id}/speakers"
    current = client.get(path)
    assert current.status_code == 200
    assert current.json() == {
        "speaker_revision": 0,
        "speakers": [
            {"speaker_number": 1, "label": "Speaker 1", "display_name": None},
            {"speaker_number": 2, "label": "Speaker 2", "display_name": None},
        ],
        "summary_job": None,
    }

    key = str(uuid4())
    headers = {"X-Idempotency-Key": key}
    renamed = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 1, "display_name": "  Ada   Lovelace  "},
        headers=headers,
    )
    assert renamed.status_code == 202
    assert renamed.json()["speaker_revision"] == 1
    assert renamed.json()["speakers"][0]["display_name"] == "Ada Lovelace"
    assert renamed.json()["summary_job"]["state"] == "queued"

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    jobs = session.query(Job).filter_by(kind=ProcessingKind.SUMMARIZATION).all()
    outbox = session.query(OutboxMessage).all()
    assert len(jobs) == 1
    assert jobs[0].speaker_revision == 1
    assert jobs[0].speaker_mapping == {"Speaker 1": "Ada Lovelace"}
    assert len(outbox) == 1
    assert outbox[0].payload["speaker_mapping"] == {"Speaker 1": "Ada Lovelace"}
    assert outbox[0].payload["speaker_revision"] == 1
    assert session.query(ArtifactManifest).filter_by(kind=ArtifactKind.SUMMARY, is_current=True).count() == 0
    assert session.get(Conversation, conversation_id).people == ["Existing person"]
    assert session.query(Note).filter_by(conversation_id=conversation_id).count() == 1
    sessions.close()

    replay = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 1, "display_name": "Ada Lovelace"},
        headers=headers,
    )
    assert replay.status_code == 202
    assert replay.json() == renamed.json()

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    first_job = session.query(Job).filter_by(kind=ProcessingKind.SUMMARIZATION).one()
    first_job.state = ProcessingState.RUNNING
    session.add(
        ProcessingAttempt(
            conversation_id=conversation_id,
            job_id=first_job.id,
            kind=ProcessingKind.SUMMARIZATION,
            attempt_number=1,
            state=ProcessingState.RUNNING,
        )
    )
    session.commit()
    sessions.close()

    renamed_again = client.patch(
        path,
        json={"expected_speaker_revision": 1, "speaker_number": 1, "display_name": "Grace Hopper"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert renamed_again.status_code == 202
    assert renamed_again.json()["speaker_revision"] == 2
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    jobs = session.query(Job).filter_by(kind=ProcessingKind.SUMMARIZATION).order_by(Job.speaker_revision).all()
    outbox = session.query(OutboxMessage).all()
    assert [(job.speaker_revision, job.speaker_mapping) for job in jobs] == [
        (1, {"Speaker 1": "Ada Lovelace"}),
        (2, {"Speaker 1": "Grace Hopper"}),
    ]
    assert jobs[0].state is ProcessingState.CANCELLED
    assert jobs[0].attempts == 1
    assert jobs[1].state is ProcessingState.QUEUED
    attempt = session.query(ProcessingAttempt).filter_by(job_id=jobs[0].id).one()
    assert attempt.state is ProcessingState.CANCELLED
    assert attempt.finished_at is not None
    assert len(outbox) == 2
    assert {message.payload["speaker_revision"]: message.published_at is not None for message in outbox} == {
        1: True,
        2: False,
    }
    sessions.close()

    stale = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 2, "display_name": "Grace Hopper"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409


@pytest.mark.unit
def test_speaker_endpoints_require_capabilities_and_hide_other_owners(client: TestClient) -> None:
    """Require the correct capabilities and conceal conversations from other owners."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    transcript = TranscriptResult(
        segments=[
            {
                "evidence_id": "segment-1",
                "speaker": "Speaker 1",
                "start_seconds": 0,
                "end_seconds": 1,
                "text": "A speaker said hello.",
            }
        ],
        provider="assemblyai",
        model="universal-3-5-pro",
    )
    content = transcript.model_dump_json().encode()
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add(
        ArtifactManifest(
            conversation_id=conversation_id,
            kind=ArtifactKind.TRANSCRIPT,
            object_key="conversations/auth-transcript.json",
            sha256=sha256(content).hexdigest(),
            byte_size=len(content),
            source_version=1,
            is_current=True,
        )
    )
    session.commit()
    sessions.close()

    class FakeStore:
        def get_bytes(self, _: str) -> bytes:
            return content

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    path = f"/v1/conversations/{conversation_id}/speakers"
    app.dependency_overrides[principal] = lambda: Principal("owner-2", frozenset({"context:read", "metadata:write"}))
    assert client.get(path).status_code == 404
    other_owner_patch = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 1, "display_name": "Ada"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert other_owner_patch.status_code == 404
    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"metadata:write"}))
    assert client.get(path).status_code == 403
    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"context:read"}))
    denied = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 1, "display_name": "Ada"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert denied.status_code == 403
    app.dependency_overrides[principal] = lambda: Principal(
        "owner-1", frozenset({"context:read", "metadata:write"})
    )
    missing_speaker = client.patch(
        path,
        json={"expected_speaker_revision": 0, "speaker_number": 2, "display_name": "Ada"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert missing_speaker.status_code == 404


@pytest.mark.unit
def test_mcp_context_omits_summary_for_stale_speaker_revision(client: TestClient) -> None:
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.get(Conversation, conversation_id).speaker_revision = 1
    session.add_all(
        [
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=1,
                object_key="conversations/mcp-transcript.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            ),
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="conversations/mcp-summary.json",
                sha256="b" * 64,
                byte_size=10,
                source_version=1,
                transcript_version=1,
                speaker_revision=0,
                is_current=True,
            ),
        ]
    )
    session.commit()

    class FakeStore:
        def get_bytes(self, key: str, limit: int | None = None) -> bytes:
            return key.encode()

    result = mcp_context(
        conversation_id,
        Principal("owner-1", frozenset({"context:read"})),
        session,
        cast(ObjectStore, FakeStore()),
    )
    sessions.close()

    assert result.transcript == "conversations/mcp-transcript.json"
    assert result.summary == ""
    assert result.artifact_versions.transcript == 1
    assert result.artifact_versions.summary is None


@pytest.mark.unit
def test_get_summary_returns_the_current_owned_artifact(client: TestClient) -> None:
    """Return the normalized current summary through the owner-authorized API."""
    conversation = create_conversation(client)
    summary = SummaryResult.model_validate(
        {
            "overview": {
                "text": "They scheduled a follow-up.",
                "evidence_span_ids": ["segment-1"],
                "confidence": "certain",
                "owner": None,
                "due_date": None,
            },
            "key_topics": [],
            "decisions": [],
            "action_items": [],
            "provider": "assemblyai",
            "model": "gemma-4-31b",
        }
    )
    payload = summary.model_dump_json().encode()
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    conversation_id = UUID(str(conversation["id"]))
    session.add_all(
        [
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=1,
                object_key="conversations/transcript.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            ),
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                object_key="conversations/summary.json",
                sha256=sha256(payload).hexdigest(),
                byte_size=len(payload),
                source_version=1,
                transcript_version=1,
                is_current=True,
            ),
        ]
    )
    session.commit()
    sessions.close()

    class FakeStore:
        def get_bytes(self, key: str) -> bytes:
            assert key == "conversations/summary.json"
            return payload

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    response = client.get(f"/v1/conversations/{conversation['id']}/summary")

    assert response.status_code == 200
    assert response.json()["overview"]["text"] == "They scheduled a follow-up."
    projection = client.get(f"/v1/conversations/{conversation['id']}")
    assert projection.json()["summary_available"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("summary_source_version", "summary_transcript_version"),
    [(2, None), (2, 4), (1, 5)],
)
def test_stale_and_legacy_summaries_are_not_current(
    client: TestClient,
    summary_source_version: int,
    summary_transcript_version: int | None,
) -> None:
    """Hide summary artifacts whose recording or transcript lineage does not match the current transcript."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add_all(
        [
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=5,
                object_key="conversations/transcript-v5.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=2,
                is_current=True,
            ),
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=4,
                object_key="conversations/obsolete-summary.json",
                sha256="b" * 64,
                byte_size=10,
                source_version=summary_source_version,
                transcript_version=summary_transcript_version,
                is_current=True,
            ),
        ]
    )
    session.commit()
    sessions.close()

    class UnusedStore:
        def get_bytes(self, _: str) -> bytes:
            raise AssertionError("obsolete summary must not be read")

    app.dependency_overrides[get_object_store] = lambda: UnusedStore()
    projection = client.get(f"/v1/conversations/{conversation_id}")
    response = client.get(f"/v1/conversations/{conversation_id}/summary")

    assert projection.status_code == 200
    assert projection.json()["summary_available"] is False
    assert response.status_code == 404


@pytest.mark.unit
def test_list_processing_jobs_returns_states_and_safe_failure_details(client: TestClient) -> None:
    """Expose durable job states and sanitized attempt errors through the owner-scoped API."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    running_job = Job(
        conversation_id=conversation_id,
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        state=ProcessingState.RUNNING,
    )
    failed_job = Job(
        conversation_id=conversation_id,
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        state=ProcessingState.FAILED,
        attempts=1,
    )
    session.add_all([running_job, failed_job])
    session.flush()
    session.add(
        ProcessingAttempt(
            conversation_id=conversation_id,
            job_id=failed_job.id,
            kind=ProcessingKind.SUMMARIZATION,
            attempt_number=1,
            state=ProcessingState.FAILED,
            error_code=ProcessingErrorCode.INVALID_INPUT,
            error_details="provider rejected the input",
        )
    )
    session.commit()
    sessions.close()

    response = client.get(f"/v1/conversations/{conversation['id']}/jobs")

    assert response.status_code == 200
    jobs = {job["kind"]: job for job in response.json()}
    assert jobs["transcription"]["state"] == "running"
    assert jobs["summarization"]["state"] == "failed"
    assert jobs["summarization"]["last_error_code"] == "invalid_input"
    assert jobs["summarization"]["last_error_details"] == "provider rejected the input"


@pytest.mark.unit
def test_recording_download_filename_uses_pacific_time_and_title() -> None:
    """Name exported recordings with a Pacific timestamp and normalized conversation title."""
    occurred_at = datetime(2026, 10, 14, 23, 32, tzinfo=timezone.utc)

    assert recording_download_filename(occurred_at, "Therapy", "mp3") == "20261014-163200--therapy.mp3"


@pytest.mark.unit
def test_revision_conflict_returns_current_values(client: TestClient) -> None:
    conversation = create_conversation(client)
    updated = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "Current title", "expected_revision": 1},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert updated.status_code == 200

    stale = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "Stale title", "expected_revision": 1},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["kind"] == "revision_conflict"


@pytest.mark.unit
def test_note_offset_and_whitespace_are_rejected(client: TestClient) -> None:
    conversation = create_conversation(client)
    response = client.post(
        f"/v1/conversations/{conversation['id']}/notes",
        json={"offset_seconds": -1, "text": "   "},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert response.status_code == 422


@pytest.mark.unit
def test_note_schemas_accept_millisecond_precision() -> None:
    """Preserve fractional-second offsets in both human and agent note requests."""
    human_note = NoteCreate.model_validate({"offset_seconds": 1.2374, "text": "API note"})
    agent_note = AgentNoteRequest.model_validate(
        {
            "conversation_id": "00000000-0000-0000-0000-000000000001",
            "offset_seconds": 1.2374,
            "text": "Agent note",
        }
    )

    assert human_note.offset_seconds == 1.237
    assert agent_note.offset_seconds == 1.237


@pytest.mark.unit
def test_note_update_schema_rejects_blank_text_and_immutable_fields() -> None:
    """Allow note text edits without accepting timestamp or authorship changes."""
    assert NoteUpdate.model_validate({"text": "Edited note"}).text == "Edited note"

    with pytest.raises(ValidationError):
        NoteUpdate.model_validate({"text": "   "})
    with pytest.raises(ValidationError):
        NoteUpdate.model_validate({"text": "Edited note", "offset_seconds": 12})


@pytest.mark.unit
@pytest.mark.parametrize("idempotency_key", [None, "", "   "])
def test_note_schema_keeps_idempotency_key_out_of_the_body(idempotency_key: str | None) -> None:
    """Keep idempotency input at the HTTP boundary rather than in note bodies."""
    payload: dict[str, object] = {"offset_seconds": 1, "text": "API note"}
    if idempotency_key is not None:
        payload["idempotency_key"] = idempotency_key

    NoteCreate.model_validate(payload)

    agent_payload = {
        "conversation_id": "00000000-0000-0000-0000-000000000001",
        "offset_seconds": 1,
        "text": "Agent note",
    }
    AgentNoteRequest.model_validate(agent_payload)


@pytest.mark.unit
@pytest.mark.parametrize("idempotency_key", ["not-a-uuid"])
def test_idempotency_header_requires_uuid(client: TestClient, idempotency_key: str) -> None:
    """Reject non-UUID idempotency keys at the HTTP boundary."""
    response = client.post(
        "/v1/conversations",
        json={"occurred_at": datetime.now(timezone.utc).isoformat()},
        headers={"X-Idempotency-Key": idempotency_key},
    )
    assert response.status_code == 422


@pytest.mark.unit
def test_repeated_note_idempotency_key_replays_existing_note(client: TestClient) -> None:
    """Replay the original note instead of creating a second note for a repeated key."""
    conversation = create_conversation(client)
    payload = {"offset_seconds": 1, "text": "First note"}

    key = str(uuid4())
    first = client.post(f"/v1/conversations/{conversation['id']}/notes", json=payload, headers={"X-Idempotency-Key": key})
    repeated = client.post(
        f"/v1/conversations/{conversation['id']}/notes",
        json={**payload, "offset_seconds": 10, "text": "Different note"}, headers={"X-Idempotency-Key": key},
    )

    assert first.status_code == 201
    assert repeated.status_code == 409
    assert repeated.json()["error"]["kind"] == "idempotency_key_reused"
    notes = client.get(f"/v1/conversations/{conversation['id']}/notes")
    assert len(notes.json()) == 1


@pytest.mark.unit
def test_human_note_update_preserves_timestamp_and_replays_idempotently(client: TestClient) -> None:
    """Update note text while preserving its timestamp, authorship, and creation time."""
    conversation = create_conversation(client)
    created = client.post(
        f"/v1/conversations/{conversation['id']}/notes",
        json={"offset_seconds": 4.237, "text": "Original note"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    original = created.json()
    note_path = f"/v1/conversations/{conversation['id']}/notes/{original['id']}"
    update_key = str(uuid4())
    headers = {"X-Idempotency-Key": update_key}
    updated = client.patch(note_path, json={"text": "Revised note"}, headers=headers)
    replay = client.patch(note_path, json={"text": "Revised note"}, headers=headers)
    mismatch = client.patch(note_path, json={"text": "Different edit"}, headers=headers)

    assert updated.status_code == 200
    assert updated.json()["text"] == "Revised note"
    assert updated.json()["offset_seconds"] == original["offset_seconds"]
    assert updated.json()["subject"] == original["subject"]
    assert updated.json()["author_kind"] == AuthorKind.HUMAN.value
    assert updated.json()["created_at"] == original["created_at"]
    assert replay.json() == updated.json()
    assert mismatch.status_code == 409


@pytest.mark.unit
def test_note_update_rejects_missing_capability_other_owner_and_agent_note(client: TestClient) -> None:
    """Hide notes from other owners, agent notes, and callers without the edit capability."""
    conversation = create_conversation(client)
    created = client.post(
        f"/v1/conversations/{conversation['id']}/notes",
        json={"offset_seconds": 1, "text": "Human note"},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    note_id = created.json()["id"]
    note_path = f"/v1/conversations/{conversation['id']}/notes/{note_id}"

    app.dependency_overrides[principal] = lambda: Principal("owner-2", frozenset({"context:read", "notes:human:edit"}))
    other_owner = client.patch(note_path, json={"text": "Changed"}, headers={"X-Idempotency-Key": str(uuid4())})
    assert other_owner.status_code == 404

    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"context:read"}))
    missing_capability = client.patch(note_path, json={"text": "Changed"}, headers={"X-Idempotency-Key": str(uuid4())})
    assert missing_capability.status_code == 403

    db_generator = app.dependency_overrides[db_session]()
    db = next(db_generator)
    try:
        note = db.get(Note, UUID(str(note_id)))
        assert note is not None
        note.author_kind = AuthorKind.AGENT
        db.commit()
    finally:
        db_generator.close()

    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"context:read", "notes:human:edit"}))
    agent_note = client.patch(note_path, json={"text": "Changed"}, headers={"X-Idempotency-Key": str(uuid4())})
    assert agent_note.status_code == 404


@pytest.mark.unit
def test_note_response_serializes_constrained_author_kinds(client: TestClient) -> None:
    """Expose human and agent author kinds as their stable wire values."""
    conversation = create_conversation(client)
    human = client.post(
        f"/v1/conversations/{conversation['id']}/notes",
        json={"offset_seconds": 4.237, "text": "Human note"}, headers={"X-Idempotency-Key": str(uuid4())},
    )

    assert human.status_code == 201
    assert human.json()["author_kind"] == AuthorKind.HUMAN.value
    assert human.json()["offset_seconds"] == 4.237
    assert human.json()["text"] == "Human note"
    assert "idempotency_key" not in human.json()

    with pytest.raises(ValidationError):
        NoteResponse.model_validate({**human.json(), "author_kind": "invalid"})


@pytest.mark.unit
def test_tombstone_prevents_stale_conversation_recreation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("muninn.api.routes.conversations.ObjectStore.delete_conversation", lambda *_: None)
    conversation = create_conversation(client)
    deleted = client.delete(f"/v1/conversations/{conversation['id']}", headers={"X-Idempotency-Key": str(uuid4())})
    assert deleted.status_code == 202
    assert deleted.json()["kind"] == "conversation_deleted"

    stale = client.post(
        "/v1/conversations",
        json={"id": conversation["id"], "occurred_at": conversation["occurred_at"]}, headers={"X-Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["kind"] == "conversation_deleted"


@pytest.mark.unit
def test_update_replays_original_response_after_conversation_changes(client: TestClient) -> None:
    """Replay a committed update even when a later update changed the resource."""
    conversation = create_conversation(client)
    key = str(uuid4())
    first = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "First title", "expected_revision": 1},
        headers={"X-Idempotency-Key": key},
    )
    later = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "Later title", "expected_revision": 2},
        headers={"X-Idempotency-Key": str(uuid4())},
    )
    replay = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "First title", "expected_revision": 1},
        headers={"X-Idempotency-Key": key},
    )

    assert first.status_code == 200
    assert later.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()


@pytest.mark.unit
def test_update_rejects_idempotency_key_fingerprint_mismatch(client: TestClient) -> None:
    """Reject reusing an update key for a different payload."""
    conversation = create_conversation(client)
    key = str(uuid4())
    first = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "First title", "expected_revision": 1},
        headers={"X-Idempotency-Key": key},
    )
    mismatch = client.patch(
        f"/v1/conversations/{conversation['id']}",
        json={"name": "Different title", "expected_revision": 1},
        headers={"X-Idempotency-Key": key},
    )

    assert first.status_code == 200
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["kind"] == "idempotency_key_reused"


@pytest.mark.unit
def test_delete_replays_tombstone_response_and_rejects_mismatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay deletion safely after tombstoning and reject a different request fingerprint."""
    monkeypatch.setattr("muninn.api.routes.conversations.ObjectStore.delete_conversation", lambda *_: None)
    conversation = create_conversation(client)
    other_conversation = create_conversation(client)
    key = str(uuid4())
    first = client.delete(f"/v1/conversations/{conversation['id']}", headers={"X-Idempotency-Key": key})
    replay = client.delete(f"/v1/conversations/{conversation['id']}", headers={"X-Idempotency-Key": key})
    mismatch = client.delete(f"/v1/conversations/{other_conversation['id']}", headers={"X-Idempotency-Key": key})

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json() == first.json()
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["kind"] == "idempotency_key_reused"


@pytest.mark.unit
def test_read_routes_require_context_read(client: TestClient) -> None:
    """Reject an authenticated owner that has no context read capability."""
    conversation = create_conversation(client)
    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"metadata:write"}))
    assert client.get("/v1/conversations").status_code == 403
    assert client.get(f"/v1/conversations/{conversation['id']}").status_code == 403
    assert client.get(f"/v1/conversations/{conversation['id']}/notes").status_code == 403


@pytest.mark.unit
def test_upload_storage_failure_does_not_create_processing_state(client: TestClient) -> None:
    """Return an unavailable error without creating an operation or upload session."""
    conversation = create_conversation(client)

    class BrokenStore:
        def __init__(self, **_: str) -> None:
            pass

        def put_stream(self, *_: object) -> object:
            raise RuntimeError("storage unavailable")

    app.dependency_overrides[get_object_store] = lambda: BrokenStore()
    response = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", b"audio", "audio/wav")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(b"audio").hexdigest()},
    )
    assert response.status_code == 503


@pytest.mark.unit
def test_upload_streams_to_store_before_committing_manifest(client: TestClient) -> None:
    """Commit processing state only after the fake object store receives the complete stream."""
    conversation = create_conversation(client)
    stored: dict[str, object] = {}

    class FakeStore:
        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
        ) -> ArtifactKind:
            stored.update(key=key, content=content.read(), length=content_length, kind=kind, content_type=content_type)
            return kind

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    payload = b"audio payload"
    response = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(payload).hexdigest()},
    )
    assert response.status_code == 201
    assert stored["content"] == payload
    assert stored["length"] == len(payload)
    assert stored["content_type"] == "audio/wav"

    projection = client.get(f"/v1/conversations/{conversation['id']}")
    assert projection.status_code == 200
    assert projection.json()["recording_available"] is True
    assert projection.json()["transcript_available"] is False


@pytest.mark.unit
def test_recording_get_streams_audio_and_supports_byte_ranges(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream a stored recording with its MIME type and standard byte-range headers."""
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    conversation = create_conversation(client)
    payload = b"stored audio bytes"
    stored: dict[str, object] = {}

    class FakeStore:
        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
        ) -> ArtifactKind:
            stored.update(key=key, content=content.read(), content_type=content_type, sha256=sha256(payload).hexdigest())
            return kind

        def get_object_metadata(self, _: str) -> dict[str, object]:
            return {"ContentLength": len(payload), "ContentType": "audio/wav"}

        def get_object_stream(self, _: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            start, end = byte_range or (0, len(payload) - 1)
            chunk = payload[start : end + 1]
            return {"Body": StreamingBody(BytesIO(chunk), len(chunk))}

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(payload).hexdigest()},
    )
    assert upload.status_code == 201

    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]
    params = {"grant": grant}
    full = client.get(
        f"/v1/conversations/{conversation['id']}/recording",
        params=params,
        headers={"Range": "bytes=0-"},
    )
    full_download = client.get(
        f"/v1/conversations/{conversation['id']}/recording",
        params={**params, "mode": "full"},
        headers={"Range": "bytes=2-5"},
    )
    partial = client.get(f"/v1/conversations/{conversation['id']}/recording", params=params, headers={"Range": "bytes=2-5"})
    invalid = client.get(f"/v1/conversations/{conversation['id']}/recording", params=params, headers={"Range": "bytes=999-1000"})
    download = client.get(
        f"/v1/conversations/{conversation['id']}/recording",
        params={**params, "download": "true"},
        headers={"Range": "bytes=2-5"},
    )

    assert full.status_code == 206
    assert full.content == payload
    assert full_download.status_code == 200
    assert full_download.content == payload
    assert full_download.headers["content-length"] == str(len(payload))
    assert full_download.headers["accept-ranges"] == "bytes"
    assert "content-range" not in full_download.headers
    assert full.headers["content-type"] == "audio/wav"
    assert partial.status_code == 206
    assert partial.content == payload[2:6]
    assert partial.headers["content-range"] == f"bytes 2-5/{len(payload)}"
    assert partial.headers["accept-ranges"] == "bytes"
    assert partial.headers["etag"].strip('"') == stored["sha256"]
    assert partial.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert invalid.status_code == 416
    assert invalid.headers["content-range"] == f"bytes */{len(payload)}"
    assert download.status_code == 206
    assert download.content == payload[2:6]
    assert download.headers["content-range"] == f"bytes 2-5/{len(payload)}"
    expected_filename = recording_download_filename(
        datetime.fromisoformat(str(conversation["occurred_at"])), str(conversation["name"]), "wav"
    )
    assert download.headers["content-disposition"] == f'attachment; filename="{expected_filename}"'

    grant_header, grant_payload, grant_signature = grant.split(".")
    tampered_signature = ("A" if grant_signature[0] != "A" else "B") + grant_signature[1:]
    tampered_grant = ".".join((grant_header, grant_payload, tampered_signature))
    assert client.get(
        f"/v1/conversations/{conversation['id']}/recording", params={"grant": tampered_grant}
    ).status_code == 404
    other_conversation = create_conversation(client)
    assert client.get(f"/v1/conversations/{other_conversation['id']}/recording", params=params).status_code == 404


@pytest.mark.unit
@pytest.mark.parametrize("acquired", [True, False])
def test_postgres_playback_build_lock_uses_nonblocking_transaction_advisory_lock(acquired: bool) -> None:
    """Use a shared PostgreSQL transaction lock and fail fast when another worker owns it."""
    calls: list[tuple[str, dict[str, int]]] = []

    class FakeResult:
        def scalar_one(self) -> bool:
            return acquired

    class FakeSession:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement: object, parameters: dict[str, int]) -> FakeResult:
            calls.append((str(statement), parameters))
            return FakeResult()

    session = cast(Session, FakeSession())
    if acquired:
        assert acquire_playback_build_lock(session, uuid4(), "owner-1") is None
    else:
        with pytest.raises(RecordingPlaybackError):
            acquire_playback_build_lock(session, uuid4(), "owner-1")
    assert "pg_try_advisory_xact_lock" in calls[0][0]
    assert calls[0][1] == {"lock_id": PLAYBACK_DERIVATIVE_ADVISORY_LOCK_ID}


@pytest.mark.unit
def test_sqlite_playback_build_lock_blocks_deletion_across_connections(tmp_path: Path) -> None:
    """Hold a SQLite write transaction across build work so another connection cannot delete."""
    engine = create_engine(f"sqlite:///{tmp_path / 'playback-lock.db'}", connect_args={"timeout": 0.1})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation = Conversation(owner_subject="owner-1", occurred_at=datetime.now(timezone.utc))
    builder = sessions()
    deleter = sessions()
    try:
        builder.add(conversation)
        builder.commit()
        acquire_playback_build_lock(builder, conversation.id, "owner-1")

        with pytest.raises(OperationalError):
            deleter.query(Conversation).filter_by(id=conversation.id).delete(synchronize_session=False)
        deleter.rollback()
        builder.commit()

        assert deleter.query(Conversation).filter_by(id=conversation.id).delete(synchronize_session=False) == 1
        deleter.commit()
        assert deleter.query(Conversation).filter_by(id=conversation.id).count() == 0
    finally:
        builder.close()
        deleter.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.unit
def test_playback_cache_fails_closed_for_unsupported_database() -> None:
    """Reject remux builds when the database has no cross-process locking strategy."""

    class FakeSession:
        def get_bind(self) -> SimpleNamespace:
            return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

    with pytest.raises(RecordingPlaybackError):
        acquire_playback_build_lock(cast(Session, FakeSession()), uuid4(), "owner-1")


@pytest.mark.unit
def test_webm_playback_remux_is_cached_range_seekable_and_keeps_originals(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use a source-scoped WebM derivative only for playback and preserve original artifacts."""
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    conversation = create_conversation(client)
    source = b"original browser WebM payload"
    derivative = b"seekable remuxed WebM payload"

    class FakeStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.content_types: dict[str, str] = {}
            self.metadata: dict[str, dict[str, str]] = {}

        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
            metadata: dict[str, str] | None = None,
        ) -> ArtifactKind:
            self.objects[key] = content.read()
            self.content_types[key] = content_type or "application/octet-stream"
            self.metadata[key] = metadata or {}
            assert len(self.objects[key]) == content_length
            return kind

        def get_object_metadata(self, key: str) -> dict[str, object]:
            if key not in self.objects:
                raise ObjectStoreNotFoundError("missing object")
            return {
                "ContentLength": len(self.objects[key]),
                "ContentType": self.content_types[key],
                "Metadata": self.metadata[key],
            }

        def get_object_stream(self, key: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            content = self.objects[key]
            start, end = byte_range or (0, len(content) - 1)
            chunk = content[start : end + 1]
            return {"Body": StreamingBody(BytesIO(chunk), len(chunk))}

    store = FakeStore()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(derivative)

    monkeypatch.setattr("muninn.api.routes.conversations.subprocess.run", fake_run)
    app.dependency_overrides[get_object_store] = lambda: store
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.webm", source, "audio/webm;codecs=opus")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(source).hexdigest()},
    )
    assert upload.status_code == 201
    source_key = next(iter(store.objects))
    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]
    playback_url = f"/v1/conversations/{conversation['id']}/recording"

    playback = client.get(playback_url, params={"grant": grant})
    ranged = client.get(
        playback_url,
        params={"grant": grant},
        headers={"Range": "bytes=3-8", "If-Range": playback.headers["etag"]},
    )
    stale_range = client.get(
        playback_url,
        params={"grant": grant},
        headers={"Range": "bytes=3-8", "If-Range": '"stale-validator"'},
    )
    original_download = client.get(playback_url, params={"grant": grant, "mode": "full"})
    original_range_download = client.get(
        playback_url,
        params={"grant": grant, "download": "true"},
        headers={"Range": "bytes=2-5"},
    )

    derivative_keys = set(store.objects) - {source_key}
    assert len(derivative_keys) == 1
    derivative_key = derivative_keys.pop()
    assert derivative_key.startswith(f"conversations/{conversation['id']}/playback/v2/")
    assert playback.content == derivative
    assert playback.headers["content-type"] == "audio/webm"
    assert playback.headers["etag"] == f'"{sha256(derivative).hexdigest()}"'
    assert store.metadata[derivative_key] == {"sha256": sha256(derivative).hexdigest()}
    assert ranged.status_code == 206
    assert ranged.content == derivative[3:9]
    assert ranged.headers["content-range"] == f"bytes 3-8/{len(derivative)}"
    assert stale_range.status_code == 200
    assert stale_range.content == derivative
    assert original_download.content == source
    assert original_range_download.status_code == 206
    assert original_range_download.content == source[2:6]
    assert store.objects[source_key] == source
    assert len(commands) == 1
    assert commands[0][0] == "prlimit"
    assert commands[0][1].startswith("--fsize=")
    assert commands[0][2] == "--core=0"
    assert commands[0][3] == "--"
    assert commands[0][4] == "ffmpeg"
    assert "-fs" not in commands[0]
    assert "-c:a" in commands[0] and "copy" in commands[0]
    input_index = commands[0].index("-i")
    protocol_index = commands[0].index("-protocol_whitelist")
    demuxer_index = commands[0].index("-f")
    assert protocol_index < input_index
    assert commands[0][protocol_index + 1] == "file"
    assert demuxer_index < input_index
    assert commands[0][demuxer_index + 1] == "matroska"

    db_generator = app.dependency_overrides[db_session]()
    db = next(db_generator)
    try:
        manifest = db.query(ArtifactManifest).filter_by(
            conversation_id=UUID(str(conversation["id"])), kind=ArtifactKind.RECORDING
        ).one()
        message = db.query(OutboxMessage).filter_by(conversation_id=UUID(str(conversation["id"]))).one()
        assert manifest.object_key == source_key
        assert manifest.sha256 == sha256(source).hexdigest()
        assert message.payload["source_key"] == source_key
    finally:
        db_generator.close()


@pytest.mark.unit
def test_webm_playback_remux_failure_preserves_original_and_returns_unavailable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail playback safely when the bounded remux subprocess times out."""
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    conversation = create_conversation(client)
    source = b"original browser WebM payload"

    class FakeStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.content_types: dict[str, str] = {}

        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
        ) -> ArtifactKind:
            self.objects[key] = content.read()
            self.content_types[key] = content_type or "application/octet-stream"
            return kind

        def get_object_metadata(self, key: str) -> dict[str, object]:
            if key not in self.objects:
                raise ObjectStoreNotFoundError("missing object")
            return {"ContentLength": len(self.objects[key]), "ContentType": self.content_types[key]}

        def get_object_stream(self, key: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            content = self.objects[key]
            return {"Body": StreamingBody(BytesIO(content), len(content))}

    store = FakeStore()

    def timeout_run(*_: object, **__: object) -> None:
        raise subprocess.TimeoutExpired("ffmpeg", 60)

    monkeypatch.setattr("muninn.api.routes.conversations.subprocess.run", timeout_run)
    app.dependency_overrides[get_object_store] = lambda: store
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.webm", source, "audio/webm")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(source).hexdigest()},
    )
    assert upload.status_code == 201
    source_key = next(iter(store.objects))
    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]

    response = client.get(f"/v1/conversations/{conversation['id']}/recording", params={"grant": grant})

    assert response.status_code == 503
    assert store.objects[source_key] == source
    assert list(store.objects) == [source_key]


@pytest.mark.unit
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is unavailable")
def test_playlist_payload_is_rejected_without_creating_a_derivative(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject playlist-shaped input under the forced Matroska demuxer without fetching URLs."""
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    conversation = create_conversation(client)
    source = b"#EXTM3U\n#EXTINF:1,external\nhttp://127.0.0.1:1/secret.ts\n#EXT-X-ENDLIST\n"

    class FakeStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.content_types: dict[str, str] = {}

        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
        ) -> ArtifactKind:
            self.objects[key] = content.read()
            self.content_types[key] = content_type or "application/octet-stream"
            return kind

        def get_object_metadata(self, key: str) -> dict[str, object]:
            if key not in self.objects:
                raise ObjectStoreNotFoundError("missing object")
            return {"ContentLength": len(self.objects[key]), "ContentType": self.content_types[key]}

        def get_object_stream(self, key: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            content = self.objects[key]
            return {"Body": StreamingBody(BytesIO(content), len(content))}

    store = FakeStore()
    app.dependency_overrides[get_object_store] = lambda: store
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.webm", source, "audio/webm")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(source).hexdigest()},
    )
    assert upload.status_code == 201
    source_key = next(iter(store.objects))
    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]

    response = client.get(f"/v1/conversations/{conversation['id']}/recording", params={"grant": grant})

    assert response.status_code == 503
    assert store.objects == {source_key: source}


@pytest.mark.unit
@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="FFmpeg tools are unavailable")
def test_generated_opus_webm_remux_has_finite_duration_and_seekable_packets(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Remux a generated WebM/Opus recording and verify duration and timestamp seeking."""
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    conversation = create_conversation(client)
    source_path = tmp_path / "source.webm"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000:duration=2",
            "-c:a",
            "libopus",
            "-b:a",
            "64k",
            "-f",
            "webm",
            str(source_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    source = source_path.read_bytes()
    source_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=format_name:stream=codec_name", "-of", "json", str(source_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    source_info = json.loads(source_probe.stdout)
    assert "webm" in source_info["format"]["format_name"]
    assert source_info["streams"][0]["codec_name"] == "opus"

    class FakeStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}
            self.content_types: dict[str, str] = {}
            self.metadata: dict[str, dict[str, str]] = {}

        def put_stream(
            self,
            key: str,
            content: BinaryIO,
            content_length: int,
            kind: ArtifactKind,
            content_type: str | None = None,
            metadata: dict[str, str] | None = None,
        ) -> ArtifactKind:
            self.objects[key] = content.read()
            self.content_types[key] = content_type or "application/octet-stream"
            self.metadata[key] = metadata or {}
            assert len(self.objects[key]) == content_length
            return kind

        def get_object_metadata(self, key: str) -> dict[str, object]:
            if key not in self.objects:
                raise ObjectStoreNotFoundError("missing object")
            return {
                "ContentLength": len(self.objects[key]),
                "ContentType": self.content_types[key],
                "Metadata": self.metadata[key],
            }

        def get_object_stream(self, key: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            content = self.objects[key]
            start, end = byte_range or (0, len(content) - 1)
            chunk = content[start : end + 1]
            return {"Body": StreamingBody(BytesIO(chunk), len(chunk))}

    store = FakeStore()
    app.dependency_overrides[get_object_store] = lambda: store
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.webm", source, "audio/webm")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(source).hexdigest()},
    )
    assert upload.status_code == 201
    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]
    response = client.get(f"/v1/conversations/{conversation['id']}/recording", params={"grant": grant})
    assert response.status_code == 200
    derivative_key = next(key for key in store.objects if key != f"conversations/{conversation['id']}/recording/{sha256(source).hexdigest()}")
    derivative_path = tmp_path / "playback.webm"
    derivative_path.write_bytes(store.objects[derivative_key])

    playback_probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(derivative_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    duration = float(json.loads(playback_probe.stdout)["format"]["duration"])
    seek_probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-read_intervals",
            "1%+0.1",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(derivative_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    seek_timestamps = [float(value) for value in seek_probe.stdout.splitlines() if value.strip()]

    assert math.isfinite(duration)
    assert 1.8 <= duration <= 2.3
    assert seek_timestamps
    assert 0.9 <= min(seek_timestamps) <= 1.2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("export_format", "content_type", "extension", "expected_encoder"),
    [("mp3", "audio/mpeg", "mp3", "libmp3lame"), ("mp4", "audio/mp4", "mp4", "aac")],
)
def test_recording_get_converts_selected_export_format(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    export_format: str,
    content_type: str,
    extension: str,
    expected_encoder: str,
) -> None:
    """Convert stored audio to the requested downloadable format."""
    conversation = create_conversation(client)
    payload = b"stored audio bytes"

    class FakeStore:
        def put_stream(self, *_: object) -> None:
            return None

        def get_object_metadata(self, _: str) -> dict[str, object]:
            return {"ContentLength": len(payload), "ContentType": "audio/wav"}

        def get_object_stream(self, _: str, byte_range: tuple[int, int] | None = None) -> dict[str, object]:
            return {"Body": StreamingBody(BytesIO(payload), len(payload))}

    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> None:
        commands.append(command)
        Path(command[-1]).write_bytes(b"converted audio")

    monkeypatch.setattr("muninn.api.routes.conversations.subprocess.run", fake_run)
    monkeypatch.setattr(service_settings, "artifact_grant_secret", "test-artifact-grant-secret")
    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    upload = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(payload).hexdigest()},
    )
    assert upload.status_code == 201

    grant = client.post(f"/v1/conversations/{conversation['id']}/recording-access").json()["grant"]
    exported = client.get(
        f"/v1/conversations/{conversation['id']}/recording", params={"format": export_format, "grant": grant}
    )

    assert exported.status_code == 200
    assert exported.content == b"converted audio"
    assert exported.headers["content-type"] == content_type
    expected_filename = recording_download_filename(
        datetime.fromisoformat(str(conversation["occurred_at"])), str(conversation["name"]), extension
    )
    assert exported.headers["content-disposition"] == f'attachment; filename="{expected_filename}"'
    assert expected_encoder in commands[0]


@pytest.mark.unit
def test_recording_get_returns_not_found_without_a_recording(client: TestClient) -> None:
    """Hide the recording endpoint when a conversation has no stored audio."""
    conversation = create_conversation(client)

    response = client.get(f"/v1/conversations/{conversation['id']}/recording")

    assert response.status_code == 404


@pytest.mark.unit
@pytest.mark.parametrize(
    "media_type",
    ["audio/wav", "audio/x-wav", "audio/mp4", "audio/aac", "audio/mpeg", "audio/webm;codecs=opus", "audio/ogg"],
)
def test_upload_accepts_supported_audio_media_types(client: TestClient, media_type: str) -> None:
    """Accept every supported imported audio format."""
    conversation = create_conversation(client)

    class FakeStore:
        def put_stream(self, *_: object) -> None:
            return None

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    payload = b"browser recording"

    response = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.audio", payload, media_type)},
        headers={"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(payload).hexdigest()},
    )

    assert response.status_code == 201


@pytest.mark.unit
def test_completed_upload_replay_does_not_read_body_or_store_again(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay a completed upload before reading its body or invoking storage."""
    conversation = create_conversation(client)
    payload = b"replayable audio"
    read_calls = 0
    store_calls = 0

    original_read = StarletteUploadFile.read

    async def tracked_read(upload: StarletteUploadFile, size: int = -1) -> bytes:
        nonlocal read_calls
        read_calls += 1
        return await original_read(upload, size)

    class FakeStore:
        def put_stream(self, *_: object) -> None:
            nonlocal store_calls
            store_calls += 1

    monkeypatch.setattr(StarletteUploadFile, "read", tracked_read)
    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    headers = {"X-Idempotency-Key": str(uuid4()), "X-Content-SHA256": sha256(payload).hexdigest()}

    first = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers=headers,
    )
    first_read_calls = read_calls
    replay = client.post(
        f"/v1/conversations/{conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers=headers,
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert first_read_calls > 0
    assert read_calls == first_read_calls
    assert store_calls == 1


@pytest.mark.unit
def test_upload_idempotency_is_scoped_to_conversation(client: TestClient) -> None:
    """Scope a key to its caller, so reusing it for another resource is rejected."""
    class FakeStore:
        def put_stream(self, *_: object) -> None:
            return None

    app.dependency_overrides[get_object_store] = lambda: FakeStore()
    first_conversation = create_conversation(client)
    second_conversation = create_conversation(client)
    payload = b"same idempotency key, distinct conversation"
    key = str(uuid4())
    headers = {"X-Idempotency-Key": key, "X-Content-SHA256": sha256(payload).hexdigest()}

    first = client.post(
        f"/v1/conversations/{first_conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers=headers,
    )
    repeated = client.post(
        f"/v1/conversations/{first_conversation['id']}/recording",
        files={"recording": ("recording.wav", b"different", "audio/wav")},
        headers={"X-Idempotency-Key": key, "X-Content-SHA256": sha256(b"different").hexdigest()},
    )
    second = client.post(
        f"/v1/conversations/{second_conversation['id']}/recording",
        files={"recording": ("recording.wav", payload, "audio/wav")},
        headers=headers,
    )

    assert first.status_code == 201
    assert repeated.status_code == 409
    assert second.status_code == 409
    assert first.json()["status"] == UploadState.COMMITTED.value


@pytest.mark.unit
def test_stage_summarization_deduplicates_active_runs_retries_and_allows_reruns(client: TestClient) -> None:
    """Deduplicate active summary jobs, retry failures, and allow deliberate reruns after success."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add_all(
        [
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=4,
                object_key="conversations/transcript-v4.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=2,
                is_current=True,
            ),
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=3,
                object_key="conversations/summary-v3.json",
                sha256="b" * 64,
                byte_size=10,
                source_version=2,
                transcript_version=3,
                is_current=True,
            ),
        ]
    )
    session.commit()
    sessions.close()

    path = f"/v1/conversations/{conversation_id}/jobs/summarization"
    first_key = str(uuid4())
    first_headers = {"X-Idempotency-Key": first_key}
    first = client.post(path, headers=first_headers)
    replay = client.post(path, headers=first_headers)
    active_duplicate = client.post(path, headers={"X-Idempotency-Key": str(uuid4())})

    assert first.status_code == replay.status_code == active_duplicate.status_code == 202
    assert first.json()["kind"] == ProcessingKind.SUMMARIZATION.value
    assert first.json()["state"] == ProcessingState.QUEUED.value
    assert replay.json() == first.json()
    assert active_duplicate.json()["id"] == first.json()["id"]

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    first_job = session.get(Job, UUID(first.json()["id"]))
    assert first_job is not None
    assert first_job.source_version == 2
    assert first_job.transcript_version == 4
    assert session.query(Job).filter_by(conversation_id=conversation_id).count() == 1
    first_outbox = session.query(OutboxMessage).filter_by(conversation_id=conversation_id).one()
    assert first_outbox.payload["source_key"] == "conversations/transcript-v4.json"
    assert first_outbox.payload["transcript_version"] == 4
    first_job.state = ProcessingState.FAILED
    session.commit()
    sessions.close()

    retry = client.post(path, headers={"X-Idempotency-Key": str(uuid4())})
    assert retry.status_code == 202
    assert retry.json()["id"] != first.json()["id"]

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    retry_job = session.get(Job, UUID(retry.json()["id"]))
    assert retry_job is not None
    retry_job.state = ProcessingState.SUCCEEDED
    session.commit()
    sessions.close()

    rerun_key = str(uuid4())
    rerun_headers = {"X-Idempotency-Key": rerun_key}
    rerun = client.post(path, headers=rerun_headers)
    rerun_replay = client.post(path, headers=rerun_headers)
    assert rerun.status_code == rerun_replay.status_code == 202
    assert rerun.json()["id"] not in {first.json()["id"], retry.json()["id"]}
    assert rerun_replay.json() == rerun.json()

    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    jobs = session.query(Job).filter_by(conversation_id=conversation_id).all()
    outboxes = session.query(OutboxMessage).filter_by(conversation_id=conversation_id).all()
    current_summaries = session.query(ArtifactManifest).filter_by(
        conversation_id=conversation_id,
        kind=ArtifactKind.SUMMARY,
        is_current=True,
    ).all()
    assert len(jobs) == 3
    assert len(outboxes) == 3
    assert len({outbox.deduplication_key for outbox in outboxes}) == 3
    assert len(current_summaries) == 1
    assert current_summaries[0].object_key == "conversations/summary-v3.json"
    sessions.close()


@pytest.mark.unit
def test_stage_summarization_requires_admin_ownership_and_a_current_transcript(client: TestClient) -> None:
    """Require processing administration, active ownership, and a committed current transcript."""
    conversation = create_conversation(client)
    path = f"/v1/conversations/{conversation['id']}/jobs/summarization"
    headers = {"X-Idempotency-Key": str(uuid4())}

    missing_transcript = client.post(path, headers=headers)
    assert missing_transcript.status_code == 404

    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset({"context:read"}))
    missing_capability = client.post(path, headers={"X-Idempotency-Key": str(uuid4())})
    assert missing_capability.status_code == 403

    app.dependency_overrides[principal] = lambda: Principal("owner-2", frozenset({"processing:admin"}))
    other_owner = client.post(path, headers={"X-Idempotency-Key": str(uuid4())})
    assert other_owner.status_code == 404


@pytest.mark.unit
def test_export_notes_returns_all_notes_in_public_schema(client: TestClient) -> None:
    """Export every active note beyond the list limit without adding private fields."""
    conversation = create_conversation(client)
    conversation_id = UUID(str(conversation["id"]))
    sessions = app.dependency_overrides[db_session]()
    session = next(sessions)
    session.add_all(
        [
            Note(
                conversation_id=conversation_id,
                subject="owner-1",
                author_kind=AuthorKind.HUMAN,
                offset_seconds=float(index),
                text=f"Note {index}",
            )
            for index in range(75)
        ]
    )
    session.commit()
    sessions.close()

    notes_path = f"/v1/conversations/{conversation_id}/notes"
    listed = client.get(notes_path)
    exported = client.get(f"{notes_path}/export")

    assert listed.status_code == exported.status_code == 200
    assert len(listed.json()) == 50
    payload = exported.json()
    assert len(payload) == 75
    assert [note["offset_seconds"] for note in payload] == list(map(float, range(75)))
    assert set(payload[0]) == set(NoteResponse.model_fields)
    assert payload[0]["text"] == "Note 0"
    assert exported.headers["content-type"].startswith("application/json")
    assert exported.headers["content-disposition"] == 'attachment; filename="notes.json"'
    assert exported.headers["cache-control"] == "private, no-store"


@pytest.mark.unit
def test_export_notes_enforces_owner_and_context_read(client: TestClient) -> None:
    """Hide another owner's exported notes and require the context-read capability."""
    conversation = create_conversation(client)
    path = f"/v1/conversations/{conversation['id']}/notes/export"

    app.dependency_overrides[principal] = lambda: Principal("owner-2", frozenset({"context:read"}))
    other_owner = client.get(path)
    assert other_owner.status_code == 404

    app.dependency_overrides[principal] = lambda: Principal("owner-1", frozenset())
    missing_capability = client.get(path)
    assert missing_capability.status_code == 403
