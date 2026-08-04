"""Conversation, recording, and note endpoints."""

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re
import subprocess
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from typing import Any, BinaryIO, Iterator, Literal, cast
from unicodedata import normalize
from uuid import UUID
from zoneinfo import ZoneInfo

import jwt
from fastapi import APIRouter, Depends, File, Header, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import and_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, aliased, sessionmaker
from starlette.background import BackgroundTask

from muninn.auth.dependencies import principal, recording_principal, require_capability
from muninn.auth.jwt import Principal
from muninn.constants import (
    AuthorKind,
    ArtifactKind,
    ErrorKind,
    ProcessingKind,
    ProcessingState,
    SyncState,
    VisibilityState,
)
from muninn.api.constants import (
    ALLOWED_RECORDING_TYPES,
    MAX_PLAYBACK_DERIVATIVE_BYTES,
    MAX_RECORDING_BYTES,
    MAX_RECORDING_SPOOL_BYTES,
    PLAYBACK_REMUX_TIMEOUT_SECONDS,
    OperationKind,
    OperationStatus,
    RECORDING_READ_CHUNK_BYTES,
    UploadState,
)
from muninn.db import db_session
from muninn.db.idempotency import IdempotencyContext
from muninn.db.models import (
    ArtifactManifest,
    AuditEvent,
    Conversation,
    ConversationSpeaker,
    Job,
    Note,
    Operation,
    OutboxMessage,
    ProcessingAttempt,
    Tombstone,
    UploadSession,
)
from muninn.job_manager.lifecycle import supersede_summarization
from muninn.api.schemas import (
    ConversationCreate,
    ConversationResponse,
    ConversationSpeakerResponse,
    ConversationUpdate,
    IdempotencyKeyHeader,
    NoteCreate,
    NoteResponse,
    NoteUpdate,
    ProcessingJobResponse,
    SpeakerMappingResponse,
    SpeakerRenameRequest,
)
from muninn.api.exceptions import (
    ConversationCleanupError,
    ConversationDeletedError,
    MediaDigestMismatchError,
    MediaTooLargeError,
    NoteOffsetOutOfRangeError,
    RecordingAccessGrantError,
    RecordingExportError,
    RecordingPlaybackError,
    RecordingUploadError,
    RevisionConflictError,
    UnsupportedMediaError,
)
from muninn.queue.contracts import OutboxJobPayload
from muninn.config import service_settings
from muninn.queue.constants import (
    NatsSubject,
    SUMMARY_OUTBOX_DEDUPLICATION_KEY,
    TRANSCRIPTION_OUTBOX_DEDUPLICATION_KEY,
)
from muninn.exceptions import MuninnError, ResourceIdConflictError, ResourceNotFoundError
from muninn.queue.exceptions import ObjectStoreNotFoundError
from muninn.schemas import SummaryResult, TranscriptResult
from muninn.storage import ObjectStore, get_object_store
from muninn.utils import request_fingerprint

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


def recording_download_filename(occurred_at: datetime, title: str, extension: str) -> str:
    """Build a Pacific-time filename from the conversation timestamp and title."""
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    timestamp = occurred_at.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y%m%d-%H%M%S")
    normalized_title = normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized_title.casefold()).strip("-") or "recording"
    return f"{timestamp}--{slug}.{extension}"


def owned_conversation(
    conversation_id: UUID, caller: Principal, db: Session, for_update: bool = False
) -> Conversation:
    """Load an active owned conversation or return a safe not-found error."""
    query = db.query(Conversation).filter_by(id=conversation_id, owner_subject=caller.subject)
    if for_update:
        query = query.with_for_update()
    item = ResourceNotFoundError.enforce_defined(query.first(), "Conversation was not found")
    ResourceNotFoundError.require_condition(item.visibility != VisibilityState.DELETED, "Conversation was not found")
    return item


def active_processing_job(
    db: Session,
    conversation_id: UUID,
    kind: ProcessingKind,
    source_version: int,
    transcript_version: int | None,
    speaker_revision: int = 0,
) -> Job | None:
    """Return the active job for one exact source lineage, if present."""
    return db.scalar(
        select(Job)
        .where(
            Job.conversation_id == conversation_id,
            Job.kind == kind,
            Job.source_version == source_version,
            Job.transcript_version == transcript_version,
            Job.speaker_revision == speaker_revision,
            Job.state.in_((ProcessingState.QUEUED, ProcessingState.RUNNING)),
        )
        .order_by(Job.created_at.desc())
        .limit(1)
    )


def conversation_response(item: Conversation, db: Session) -> ConversationResponse:
    """Build a conversation response from current manifests without ORM projections."""
    current_manifests = {
        manifest.kind: manifest
        for manifest in db.scalars(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == item.id,
                ArtifactManifest.is_current.is_(True),
            )
        )
    }
    transcript = current_manifests.get(ArtifactKind.TRANSCRIPT)
    summary = current_manifests.get(ArtifactKind.SUMMARY)
    values = dict(item.__dict__)
    values.update(
        recording_available=ArtifactKind.RECORDING in current_manifests,
        transcript_available=transcript is not None,
        summary_available=(
            summary is not None
            and transcript is not None
            and summary.source_version == transcript.source_version
            and summary.transcript_version == transcript.version
            and summary.speaker_revision == item.speaker_revision
        ),
    )
    return ConversationResponse.model_validate(values)


def current_transcript(
    db: Session, store: ObjectStore, conversation_id: UUID, for_update: bool = False
) -> tuple[ArtifactManifest, TranscriptResult]:
    """Load and validate the current immutable transcript artifact."""
    query = select(ArtifactManifest).where(
        ArtifactManifest.conversation_id == conversation_id,
        ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
        ArtifactManifest.is_current.is_(True),
    )
    if for_update:
        query = query.with_for_update()
    manifest = ResourceNotFoundError.enforce_defined(db.scalar(query), "Transcript was not found")
    try:
        content = store.get_bytes(manifest.object_key)
    except ObjectStoreNotFoundError as exc:
        raise ResourceNotFoundError("Transcript was not found") from exc
    return manifest, TranscriptResult.model_validate_json(content)


def transcript_speaker_numbers(transcript: TranscriptResult) -> list[int]:
    """Return distinct canonical speaker numbers in transcript order."""
    return sorted({int(segment.speaker.removeprefix("Speaker ")) for segment in transcript.segments})


def speaker_mapping_snapshot(
    db: Session, conversation_id: UUID, speaker_numbers: list[int]
) -> dict[str, str]:
    """Build a snapshot of custom names limited to the current transcript speakers."""
    rows = db.scalars(
        select(ConversationSpeaker).where(
            ConversationSpeaker.conversation_id == conversation_id,
            ConversationSpeaker.speaker_number.in_(speaker_numbers),
        )
    )
    return {
        f"Speaker {row.speaker_number}": row.display_name
        for row in rows
        if row.display_name is not None
    }


def speaker_mapping_response(
    conversation: Conversation,
    transcript: TranscriptResult,
    db: Session,
    summary_job: Job | None = None,
) -> SpeakerMappingResponse:
    """Build the public transcript-local speaker mapping projection."""
    numbers = transcript_speaker_numbers(transcript)
    rows = {
        row.speaker_number: row.display_name
        for row in db.scalars(
            select(ConversationSpeaker).where(
                ConversationSpeaker.conversation_id == conversation.id,
                ConversationSpeaker.speaker_number.in_(numbers),
            )
        )
    }
    return SpeakerMappingResponse(
        speaker_revision=conversation.speaker_revision,
        speakers=[
            ConversationSpeakerResponse(
                speaker_number=number,
                label=f"Speaker {number}",
                display_name=rows.get(number),
            )
            for number in numbers
        ],
        summary_job=ProcessingJobResponse.model_validate(summary_job) if summary_job is not None else None,
    )


def stream_notes_json(export_sessions: sessionmaker[Session], conversation_id: UUID) -> Iterator[bytes]:
    """Stream every active note with bounded database fetches and the public note schema."""
    with export_sessions() as export_db:
        notes = export_db.scalars(
            select(Note)
            .where(Note.conversation_id == conversation_id, Note.deleted_at.is_(None))
            .order_by(Note.offset_seconds, Note.id)
            .execution_options(yield_per=128)
        )
        yield b"["
        for index, note in enumerate(notes):
            if index:
                yield b","
            yield NoteResponse.model_validate(note).model_dump_json().encode("utf-8")
        yield b"]"


def parse_single_byte_range(range_header: str | None, size: int) -> tuple[int, int] | None:
    """Parse one byte range into inclusive start and end offsets."""
    if range_header is None:
        return None
    try:
        unit, value = range_header.strip().split("=", 1)
        if unit.lower() != "bytes" or "," in value:
            raise ValueError
        first, last = value.split("-", 1)
        if first:
            if not first.isdecimal() or (last and not last.isdecimal()):
                raise ValueError
            start = int(first)
            end = int(last) if last else size - 1
        else:
            if not last.isdecimal():
                raise ValueError
            suffix_length = int(last)
            if suffix_length <= 0:
                raise ValueError
            start = max(size - suffix_length, 0)
            end = size - 1
        if start >= size or end < start:
            raise ValueError
        return start, min(end, size - 1)
    except ValueError:
        raise ValueError("Invalid byte range") from None


def stream_object_body(body: Any) -> Iterator[bytes]:
    """Yield stored audio in chunks and release the object-store connection."""
    try:
        while chunk := body.read(RECORDING_READ_CHUNK_BYTES):
            yield chunk
    finally:
        body.close()


PLAYBACK_DERIVATIVE_ADVISORY_LOCK_ID = 0x4D554E494E4E01


def acquire_playback_build_lock(db: Session, conversation_id: UUID, owner_subject: str) -> None:
    """Acquire a database-backed remux lock that coordinates every API process."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        acquired = db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": PLAYBACK_DERIVATIVE_ADVISORY_LOCK_ID},
        ).scalar_one()
        if not acquired:
            raise RecordingPlaybackError("Recording playback is busy")
        return
    if dialect == "sqlite":
        result = db.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.owner_subject == owner_subject,
                Conversation.visibility == VisibilityState.ACTIVE,
            )
            .values(revision=Conversation.revision)
        )
        if result.rowcount != 1:
            raise ResourceNotFoundError("Recording was not found")
        return
    raise RecordingPlaybackError("Recording playback is unavailable for this database")


def verify_playback_build_lock(db: Session, conversation_id: UUID, source_key: str) -> None:
    """Revalidate the locked conversation and source before publishing a derivative."""
    conversation = db.query(Conversation).filter_by(
        id=conversation_id,
        visibility=VisibilityState.ACTIVE,
    ).with_for_update().first()
    if conversation is None:
        raise ResourceNotFoundError("Recording was not found")
    manifest = db.query(ArtifactManifest).filter_by(
        conversation_id=conversation_id,
        kind=ArtifactKind.RECORDING,
        is_current=True,
    ).first()
    if manifest is None or manifest.object_key != source_key:
        raise ResourceNotFoundError("Recording was not found")
    if db.get_bind().dialect.name == "postgresql":
        acquired = db.execute(
            text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
            {"lock_id": PLAYBACK_DERIVATIVE_ADVISORY_LOCK_ID},
        ).scalar_one()
        if not acquired:
            raise RecordingPlaybackError("Recording playback is busy")


def playback_derivative_key(conversation_id: UUID, source_sha256: str) -> str:
    """Build a versioned cache key scoped to one conversation recording digest."""
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise RecordingPlaybackError("Recording playback failed")
    return f"conversations/{conversation_id}/playback/v2/{source_sha256}.webm"


def get_playback_derivative(
    db: Session,
    store: ObjectStore,
    conversation_id: UUID,
    owner_subject: str,
    source_key: str,
    source_sha256: str,
    source_size: int,
) -> tuple[str, dict[str, Any]]:
    """Return a cached seekable WebM remux or create it from a verified source."""
    key = playback_derivative_key(conversation_id, source_sha256)

    def cached_metadata() -> dict[str, Any] | None:
        try:
            metadata = store.get_object_metadata(key)
        except ObjectStoreNotFoundError:
            return None
        size = int(metadata.get("ContentLength", 0))
        object_metadata = metadata.get("Metadata")
        derivative_sha256 = object_metadata.get("sha256") if isinstance(object_metadata, dict) else None
        if (
            size <= 0
            or size >= MAX_PLAYBACK_DERIVATIVE_BYTES
            or metadata.get("ContentType") != "audio/webm"
            or not isinstance(derivative_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", derivative_sha256) is None
        ):
            raise RecordingPlaybackError("Recording playback failed")
        return metadata

    try:
        metadata = cached_metadata()
    except Exception as exc:
        raise RecordingPlaybackError("Recording playback failed") from exc
    if metadata is not None:
        return key, metadata
    try:
        acquire_playback_build_lock(db, conversation_id, owner_subject)
        metadata = cached_metadata()
        if metadata is not None:
            return key, metadata
        if source_size <= 0 or source_size > MAX_RECORDING_BYTES:
            raise RecordingPlaybackError("Recording playback failed")
        with TemporaryDirectory(prefix="muninn-playback-") as temporary:
            source_path = Path(temporary) / "source.webm"
            derivative_path = Path(temporary) / "playback.webm"
            source = store.get_object_stream(source_key, None)["Body"]
            digest = sha256()
            size = 0
            try:
                with source_path.open("wb") as source_file:
                    while chunk := source.read(RECORDING_READ_CHUNK_BYTES):
                        size += len(chunk)
                        if size > source_size or size > MAX_RECORDING_BYTES:
                            raise RecordingPlaybackError("Recording playback failed")
                        digest.update(chunk)
                        source_file.write(chunk)
            finally:
                source.close()
            if size != source_size or digest.hexdigest() != source_sha256:
                raise RecordingPlaybackError("Recording playback failed")
            subprocess.run(
                [
                    "prlimit",
                    f"--fsize={MAX_PLAYBACK_DERIVATIVE_BYTES}",
                    "--core=0",
                    "--",
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-xerror",
                    "-y",
                    "-protocol_whitelist",
                    "file",
                    "-f",
                    "matroska",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "-1",
                    "-bitexact",
                    "-vn",
                    "-c:a",
                    "copy",
                    "-threads",
                    "1",
                    "-f",
                    "webm",
                    str(derivative_path),
                ],
                check=True,
                timeout=PLAYBACK_REMUX_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            derivative_size = derivative_path.stat().st_size
            if derivative_size <= 0 or derivative_size >= MAX_PLAYBACK_DERIVATIVE_BYTES:
                raise RecordingPlaybackError("Recording playback failed")
            derivative_digest = sha256()
            with derivative_path.open("rb") as derivative_file:
                while chunk := derivative_file.read(RECORDING_READ_CHUNK_BYTES):
                    derivative_digest.update(chunk)
                derivative_sha256 = derivative_digest.hexdigest()
                verify_playback_build_lock(db, conversation_id, source_key)
                derivative_file.seek(0)
                store.put_stream(
                    key,
                    derivative_file,
                    derivative_size,
                    ArtifactKind.RECORDING,
                    "audio/webm",
                    metadata={"sha256": derivative_sha256},
                )
        return key, {
            "ContentLength": derivative_size,
            "ContentType": "audio/webm",
            "Metadata": {"sha256": derivative_sha256},
        }
    except (ObjectStoreNotFoundError, RecordingPlaybackError, ResourceNotFoundError):
        raise
    except Exception as exc:
        raise RecordingPlaybackError("Recording playback failed") from exc


ExportFormat = Literal["aac", "m4a", "mp3", "mp4", "ogg", "wav", "webm"]
EXPORT_FORMATS: dict[ExportFormat, tuple[str, str, tuple[str, ...]]] = {
    "aac": ("audio/aac", "aac", ("-c:a", "aac", "-f", "adts")),
    "m4a": ("audio/mp4", "m4a", ("-c:a", "aac", "-f", "ipod")),
    "mp3": ("audio/mpeg", "mp3", ("-c:a", "libmp3lame")),
    "mp4": ("audio/mp4", "mp4", ("-c:a", "aac", "-f", "mp4")),
    "ogg": ("audio/ogg", "ogg", ("-c:a", "libvorbis", "-f", "ogg")),
    "wav": ("audio/wav", "wav", ("-c:a", "pcm_s16le", "-f", "wav")),
    "webm": ("audio/webm", "webm", ("-c:a", "libopus", "-f", "webm")),
}


def stream_export_file(path: Path) -> Iterator[bytes]:
    """Yield a converted recording and close its file handle."""
    with path.open("rb") as converted:
        while chunk := converted.read(RECORDING_READ_CHUNK_BYTES):
            yield chunk


def export_recording(
    store: ObjectStore, object_key: str, export_format: ExportFormat, download_filename: str
) -> Response:
    """Convert a stored recording and stream it as a downloadable file."""
    content_type, extension, encoding_args = EXPORT_FORMATS[export_format]
    temporary = TemporaryDirectory(prefix="muninn-export-")
    source_path = Path(temporary.name) / "source.audio"
    converted_path = Path(temporary.name) / f"recording.{extension}"
    try:
        with RecordingExportError.handle_errors("Recording export failed"):
            stored = store.get_object_stream(object_key, None)
            with source_path.open("wb") as source:
                for chunk in stream_object_body(stored["Body"]):
                    source.write(chunk)
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    *encoding_args,
                    str(converted_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            content_length = converted_path.stat().st_size
    except Exception:
        temporary.cleanup()
        raise

    return StreamingResponse(
        stream_export_file(converted_path),
        media_type=content_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{download_filename}"',
            "Content-Length": str(content_length),
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(temporary.cleanup),
    )


@router.post("", response_model=ConversationResponse, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
):
    """
    Create or replay an owned conversation identity.

    - Authenticate the caller, validate the request payload, and fingerprint and reserve the
      idempotency key.
    - On replay, load the recorded conversation, verify that it is still owned and active,
      and return its current response.
    - Validate an optional client-supplied ID against tombstones and existing resource IDs.
    - Insert the conversation owned by the caller and flush it to obtain its ID.
    - Complete the idempotency record with the conversation ID and commit the database transaction.

    Return the created or replayed conversation, including its current artifact availability.
    """
    require_capability("metadata:write", caller)
    fingerprint = request_fingerprint(payload.model_dump(mode="json"))
    with IdempotencyContext(db, caller.subject, "conversation_create", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            resource_id = ResourceNotFoundError.enforce_defined(idempotency.resource_id, "Conversation was not found")
            return conversation_response(owned_conversation(resource_id, caller, db), db)

        item: Conversation | None = None
        if payload.id:
            ConversationDeletedError.require_condition(
                db.get(Tombstone, payload.id) is None,
                "Conversation has been deleted",
            )
            item = db.get(Conversation, payload.id)
            ResourceIdConflictError.require_condition(item is None, "Resource ID is already in use")

        item = Conversation(owner_subject=caller.subject, **payload.model_dump(exclude={"id"}))
        if payload.id:
            item.id = payload.id
        db.add(item)
        db.flush()
        idempotency.capture(item.id)
    db.commit()
    return conversation_response(item, db)


@router.get("", response_model=list[ConversationResponse])
def list_conversations(caller: Principal = Depends(principal), db: Session = Depends(db_session)):
    """List active owned conversations newest first."""
    require_capability("context:read", caller)
    items = db.query(Conversation).filter_by(owner_subject=caller.subject, visibility=VisibilityState.ACTIVE)
    return [conversation_response(item, db) for item in items.order_by(Conversation.occurred_at.desc()).all()]


@router.get("/{conversation_id}", response_model=ConversationResponse)
def get_conversation(conversation_id: UUID, caller: Principal = Depends(principal), db: Session = Depends(db_session)):
    """Return one owned conversation."""
    require_capability("context:read", caller)
    return conversation_response(owned_conversation(conversation_id, caller, db), db)


@router.get("/{conversation_id}/jobs", response_model=list[ProcessingJobResponse])
def list_processing_jobs(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
) -> list[ProcessingJobResponse]:
    """List processing jobs for one owned conversation, newest first."""
    require_capability("context:read", caller)
    owned_conversation(conversation_id, caller, db)
    jobs = db.query(Job).filter_by(conversation_id=conversation_id).order_by(Job.created_at.desc()).all()
    attempts = db.query(ProcessingAttempt).filter(
        ProcessingAttempt.job_id.in_([job.id for job in jobs])
    ).order_by(ProcessingAttempt.attempt_number.desc()).all() if jobs else []
    latest_attempts = {attempt.job_id: attempt for attempt in reversed(attempts)}
    return [
        ProcessingJobResponse.model_validate(job).model_copy(
            update={
                "last_error_code": latest_attempts[job.id].error_code if job.id in latest_attempts else None,
                "last_error_details": latest_attempts[job.id].error_details if job.id in latest_attempts else None,
            }
        )
        for job in jobs
    ]


@router.post("/{conversation_id}/jobs/transcription", response_model=ProcessingJobResponse, status_code=202)
def stage_transcription_job(
    conversation_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
) -> ProcessingJobResponse:
    """Stage one transcription job when no current job can produce the transcript."""
    require_capability("processing:admin", caller)
    fingerprint = request_fingerprint({"conversation_id": str(conversation_id), "kind": ProcessingKind.TRANSCRIPTION.value})
    response: ProcessingJobResponse
    with IdempotencyContext(db, caller.subject, "transcription_stage", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            if idempotency.response_data is not None:
                return ProcessingJobResponse.model_validate(idempotency.response_data)
            replay_job = db.get(Job, idempotency.resource_id)
            return ProcessingJobResponse.model_validate(ResourceNotFoundError.enforce_defined(replay_job, "Transcription job was not found"))

        conversation = db.query(Conversation).filter_by(
            id=conversation_id,
            owner_subject=caller.subject,
            visibility=VisibilityState.ACTIVE,
        ).with_for_update().first()
        if conversation is None:
            raise ResourceNotFoundError("Conversation was not found")

        recording = db.query(ArtifactManifest).filter_by(
            conversation_id=conversation_id,
            kind=ArtifactKind.RECORDING,
            is_current=True,
        ).first()
        if recording is None:
            raise ResourceNotFoundError("Recording was not found")

        latest_job = db.query(Job).filter_by(
            conversation_id=conversation_id,
            kind=ProcessingKind.TRANSCRIPTION,
            source_version=recording.source_version,
        ).order_by(Job.created_at.desc()).first()
        current_transcript = db.query(ArtifactManifest).filter_by(
            conversation_id=conversation_id,
            kind=ArtifactKind.TRANSCRIPT,
            source_version=recording.source_version,
            is_current=True,
        ).first()
        if latest_job is not None and (
            latest_job.state in {ProcessingState.QUEUED, ProcessingState.RUNNING}
            or latest_job.state is ProcessingState.SUCCEEDED and current_transcript is not None
        ):
            response = ProcessingJobResponse.model_validate(latest_job)
            idempotency.capture(latest_job.id, response.model_dump(mode="json"))
        else:
            job = Job(
                conversation_id=conversation_id,
                kind=ProcessingKind.TRANSCRIPTION,
                source_version=recording.source_version,
            )
            try:
                with db.begin_nested():
                    db.add(job)
                    db.flush()
            except IntegrityError:
                job = active_processing_job(
                    db, conversation_id, ProcessingKind.TRANSCRIPTION, recording.source_version, None
                )
                if job is None:
                    raise
            else:
                outbox_payload = OutboxJobPayload(
                    job_id=job.id,
                    conversation_id=conversation_id,
                    kind=ProcessingKind.TRANSCRIPTION,
                    source_version=recording.source_version,
                    source_key=recording.object_key,
                )
                db.add(
                    OutboxMessage(
                        conversation_id=conversation_id,
                        subject=NatsSubject.JOBS,
                        payload=outbox_payload.model_dump(mode="json", exclude_none=True),
                        deduplication_key=TRANSCRIPTION_OUTBOX_DEDUPLICATION_KEY.format(
                            conversation_id=conversation_id,
                            job_id=job.id,
                        ),
                    )
                )
            conversation.sync_state = SyncState.PROCESSING
            response = ProcessingJobResponse.model_validate(job)
            idempotency.capture(job.id, response.model_dump(mode="json"))
    db.commit()
    return response


@router.post("/{conversation_id}/jobs/summarization", response_model=ProcessingJobResponse, status_code=202)
def stage_summarization_job(
    conversation_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
) -> ProcessingJobResponse:
    """Stage a new summary run for the current committed transcript."""
    require_capability("processing:admin", caller)
    fingerprint = request_fingerprint(
        {"conversation_id": str(conversation_id), "kind": ProcessingKind.SUMMARIZATION.value}
    )
    response: ProcessingJobResponse
    with IdempotencyContext(db, caller.subject, "summarization_stage", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            owned_conversation(conversation_id, caller, db)
            if idempotency.response_data is not None:
                return ProcessingJobResponse.model_validate(idempotency.response_data)
            replay_job = db.get(Job, idempotency.resource_id)
            return ProcessingJobResponse.model_validate(
                ResourceNotFoundError.enforce_defined(replay_job, "Summarization job was not found")
            )

        conversation = db.query(Conversation).filter_by(
            id=conversation_id,
            owner_subject=caller.subject,
            visibility=VisibilityState.ACTIVE,
        ).with_for_update().first()
        if conversation is None:
            raise ResourceNotFoundError("Conversation was not found")

        transcript = db.query(ArtifactManifest).filter_by(
            conversation_id=conversation_id,
            kind=ArtifactKind.TRANSCRIPT,
            is_current=True,
        ).first()
        if transcript is None:
            raise ResourceNotFoundError("Transcript was not found")

        mapping_rows = db.scalars(
            select(ConversationSpeaker).where(ConversationSpeaker.conversation_id == conversation_id)
        )
        speaker_mapping = {
            f"Speaker {row.speaker_number}": row.display_name
            for row in mapping_rows
            if row.display_name is not None
        }
        job = active_processing_job(
            db,
            conversation_id,
            ProcessingKind.SUMMARIZATION,
            transcript.source_version,
            transcript.version,
            conversation.speaker_revision,
        )
        if job is None:
            job = Job(
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=transcript.source_version,
                transcript_version=transcript.version,
                speaker_revision=conversation.speaker_revision,
                speaker_mapping=speaker_mapping,
            )
            try:
                with db.begin_nested():
                    db.add(job)
                    db.flush()
            except IntegrityError:
                job = active_processing_job(
                    db,
                    conversation_id,
                    ProcessingKind.SUMMARIZATION,
                    transcript.source_version,
                    transcript.version,
                    conversation.speaker_revision,
                )
                if job is None:
                    raise
            else:
                outbox_payload = OutboxJobPayload(
                    job_id=job.id,
                    conversation_id=conversation_id,
                    kind=ProcessingKind.SUMMARIZATION,
                    source_version=transcript.source_version,
                    transcript_version=transcript.version,
                    speaker_revision=conversation.speaker_revision,
                    speaker_mapping=speaker_mapping,
                    source_key=transcript.object_key,
                )
                db.add(
                    OutboxMessage(
                        conversation_id=conversation_id,
                        subject=NatsSubject.JOBS,
                        payload=outbox_payload.model_dump(mode="json", exclude_none=True),
                        deduplication_key=SUMMARY_OUTBOX_DEDUPLICATION_KEY.format(
                            conversation_id=conversation_id,
                            source_version=transcript.source_version,
                            transcript_version=transcript.version,
                            speaker_revision=conversation.speaker_revision,
                            job_id=job.id,
                        ),
                    )
                )
        conversation.sync_state = SyncState.PROCESSING
        response = ProcessingJobResponse.model_validate(job)
        idempotency.capture(job.id, response.model_dump(mode="json"))
    db.commit()
    return response


@router.get("/{conversation_id}/transcript", response_model=TranscriptResult)
def get_transcript(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> TranscriptResult:
    """Return the current transcript for one owned conversation."""
    require_capability("context:read", caller)
    owned_conversation(conversation_id, caller, db)
    manifest = db.query(ArtifactManifest).filter_by(
        conversation_id=conversation_id,
        kind=ArtifactKind.TRANSCRIPT,
        is_current=True,
    ).first()
    if manifest is None:
        raise ResourceNotFoundError("Transcript was not found")
    try:
        content = store.get_bytes(manifest.object_key)
    except ObjectStoreNotFoundError as exc:
        raise ResourceNotFoundError("Transcript was not found") from exc
    return TranscriptResult.model_validate_json(content)


@router.get("/{conversation_id}/speakers", response_model=SpeakerMappingResponse)
def get_conversation_speakers(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> SpeakerMappingResponse:
    """Return current transcript speakers and their conversation-local names."""
    require_capability("context:read", caller)
    conversation = owned_conversation(conversation_id, caller, db, for_update=True)
    _, transcript = current_transcript(db, store, conversation_id, for_update=True)
    return speaker_mapping_response(conversation, transcript, db)


@router.patch("/{conversation_id}/speakers", response_model=SpeakerMappingResponse, status_code=202)
def rename_conversation_speaker(
    conversation_id: UUID,
    payload: SpeakerRenameRequest,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> SpeakerMappingResponse:
    """Rename one current transcript speaker and enqueue its mapped summary atomically."""
    require_capability("metadata:write", caller)
    fingerprint = request_fingerprint(
        {"conversation_id": str(conversation_id), **payload.model_dump(mode="json")}
    )
    if db.get_bind().dialect.name == "sqlite":
        db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(speaker_revision=Conversation.speaker_revision)
        )
    with IdempotencyContext(db, caller.subject, "speaker_rename", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            owned_conversation(conversation_id, caller, db)
            return SpeakerMappingResponse.model_validate(
                ResourceNotFoundError.enforce_defined(
                    idempotency.response_data, "Speaker mapping response was not found"
                )
            )

        conversation = owned_conversation(conversation_id, caller, db, for_update=True)
        RevisionConflictError.require_condition(
            payload.expected_speaker_revision == conversation.speaker_revision,
            "Conversation speaker revision conflict",
        )
        transcript_manifest, transcript = current_transcript(db, store, conversation_id, for_update=True)
        speaker_numbers = transcript_speaker_numbers(transcript)
        ResourceNotFoundError.require_condition(
            payload.speaker_number in speaker_numbers,
            "Transcript speaker was not found",
        )
        speaker = db.scalar(
            select(ConversationSpeaker).where(
                ConversationSpeaker.conversation_id == conversation_id,
                ConversationSpeaker.speaker_number == payload.speaker_number,
            )
        )
        if speaker is None:
            speaker = ConversationSpeaker(
                conversation_id=conversation_id,
                speaker_number=payload.speaker_number,
            )
            db.add(speaker)
        summary_job = None
        if speaker.display_name != payload.display_name:
            supersede_summarization(db, conversation_id)
            speaker.display_name = payload.display_name
            conversation.speaker_revision += 1
            speaker_mapping = speaker_mapping_snapshot(db, conversation_id, speaker_numbers)
            db.execute(
                update(ArtifactManifest)
                .where(
                    ArtifactManifest.conversation_id == conversation_id,
                    ArtifactManifest.kind == ArtifactKind.SUMMARY,
                    ArtifactManifest.is_current.is_(True),
                )
                .values(is_current=False)
            )
            summary_job = Job(
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=transcript_manifest.source_version,
                transcript_version=transcript_manifest.version,
                speaker_revision=conversation.speaker_revision,
                speaker_mapping=speaker_mapping,
            )
            db.add(summary_job)
            db.flush()
            outbox_payload = OutboxJobPayload(
                job_id=summary_job.id,
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=transcript_manifest.source_version,
                transcript_version=transcript_manifest.version,
                speaker_revision=conversation.speaker_revision,
                speaker_mapping=speaker_mapping,
                source_key=transcript_manifest.object_key,
            )
            db.add(
                OutboxMessage(
                    conversation_id=conversation_id,
                    subject=NatsSubject.JOBS,
                    payload=outbox_payload.model_dump(mode="json", exclude_none=True),
                    deduplication_key=SUMMARY_OUTBOX_DEDUPLICATION_KEY.format(
                        conversation_id=conversation_id,
                        source_version=transcript_manifest.source_version,
                        transcript_version=transcript_manifest.version,
                        speaker_revision=conversation.speaker_revision,
                        job_id=summary_job.id,
                    ),
                )
            )
            conversation.sync_state = SyncState.PROCESSING
        db.flush()
        response = speaker_mapping_response(conversation, transcript, db, summary_job)
        idempotency.capture(conversation.id, response.model_dump(mode="json"))
    db.commit()
    return response


@router.get("/{conversation_id}/summary", response_model=SummaryResult)
def get_summary(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> SummaryResult:
    """Return the current summary for one owned conversation."""
    require_capability("context:read", caller)
    conversation = owned_conversation(conversation_id, caller, db)
    summary_manifest = aliased(ArtifactManifest)
    transcript_manifest = aliased(ArtifactManifest)
    manifest = db.execute(
        select(summary_manifest)
        .join(
            transcript_manifest,
            and_(
                transcript_manifest.conversation_id == summary_manifest.conversation_id,
                transcript_manifest.kind == ArtifactKind.TRANSCRIPT,
                transcript_manifest.is_current.is_(True),
                transcript_manifest.source_version == summary_manifest.source_version,
                transcript_manifest.version == summary_manifest.transcript_version,
            ),
        )
        .where(
            summary_manifest.conversation_id == conversation_id,
            summary_manifest.kind == ArtifactKind.SUMMARY,
            summary_manifest.is_current.is_(True),
            summary_manifest.speaker_revision == conversation.speaker_revision,
        )
    ).scalar_one_or_none()
    if manifest is None:
        raise ResourceNotFoundError("Summary was not found")
    try:
        content = store.get_bytes(manifest.object_key)
    except ObjectStoreNotFoundError as exc:
        raise ResourceNotFoundError("Summary was not found") from exc
    return SummaryResult.model_validate_json(content)


@router.post("/{conversation_id}/recording-access")
def create_recording_access_grant(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
) -> dict[str, str]:
    """Issue a short-lived playback URL token for native browser media requests."""
    require_capability("context:read", caller)
    conversation = owned_conversation(conversation_id, caller, db)
    manifest = db.query(ArtifactManifest).filter_by(
        conversation_id=conversation_id,
        kind=ArtifactKind.RECORDING,
        is_current=True,
    ).first()
    if manifest is None:
        raise ResourceNotFoundError("Recording was not found")
    if service_settings.artifact_grant_secret is None:
        raise RecordingAccessGrantError("Recording access is unavailable")

    token = jwt.encode(
        {
            "sub": caller.subject,
            "conversation_id": str(conversation_id),
            "artifact_key": manifest.object_key,
            "conversation_revision": conversation.revision,
            "capability": "context:read",
            "aud": "muninn-recording",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        },
        service_settings.artifact_grant_secret,
        algorithm="HS256",
    )
    return {"grant": token}


@router.get("/{conversation_id}/recording")
def get_recording(
    conversation_id: UUID,
    range_header: str | None = Header(default=None, alias="Range"),
    if_range: str | None = Header(default=None, alias="If-Range"),
    mode: Literal["stream", "full"] = Query(default="stream"),
    download: bool = Query(default=False),
    export_format: ExportFormat | None = Query(default=None, alias="format"),
    grant: str | None = Query(default=None),
    caller: Principal = Depends(recording_principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> Response:
    """Stream the current owned recording with optional byte-range support."""
    require_capability("context:read", caller)
    conversation = owned_conversation(conversation_id, caller, db)
    manifest = db.query(ArtifactManifest).filter_by(
        conversation_id=conversation_id,
        kind=ArtifactKind.RECORDING,
        is_current=True,
    ).first()
    if manifest is None:
        raise ResourceNotFoundError("Recording was not found")
    claims: dict[str, Any] | None = None
    if grant is not None:
        try:
            claims = jwt.decode(
                grant,
                service_settings.artifact_grant_secret,
                algorithms=["HS256"],
                audience="muninn-recording",
                options={"require": ["artifact_key", "conversation_revision"]},
            )
        except Exception as exc:
            raise ResourceNotFoundError("Recording was not found") from exc
        if (
            claims["artifact_key"] != manifest.object_key
            or claims["conversation_revision"] != conversation.revision
        ):
            raise ResourceNotFoundError("Recording was not found")
    try:
        metadata = store.get_object_metadata(manifest.object_key)
    except ObjectStoreNotFoundError as exc:
        raise ResourceNotFoundError("Recording was not found") from exc
    source_size = int(metadata["ContentLength"])
    if export_format is not None:
        if range_header is not None:
            return Response(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE)
        download_filename = recording_download_filename(
            conversation.occurred_at, conversation.name, EXPORT_FORMATS[export_format][1]
        )
        return export_recording(store, manifest.object_key, export_format, download_filename)
    full_download = mode == "full"
    content_type = str(metadata.get("ContentType") or "audio/webm")
    stream_key = manifest.object_key
    stream_size = source_size
    stream_etag = f'"{manifest.sha256}"'
    playback_db_lock = False
    if content_type == "audio/webm" and not full_download and not download:
        try:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET LOCAL lock_timeout = '10s'"))
            conversation = owned_conversation(conversation_id, caller, db, for_update=True)
            playback_db_lock = True
            current_manifest = db.query(ArtifactManifest).filter_by(
                conversation_id=conversation_id,
                kind=ArtifactKind.RECORDING,
                is_current=True,
            ).first()
            if current_manifest is None or current_manifest.object_key != manifest.object_key:
                raise ResourceNotFoundError("Recording was not found")
            if claims is not None and (
                claims["artifact_key"] != current_manifest.object_key
                or claims["conversation_revision"] != conversation.revision
            ):
                raise ResourceNotFoundError("Recording was not found")
            manifest = current_manifest
            stream_key, playback_metadata = get_playback_derivative(
                db, store, conversation_id, caller.subject, manifest.object_key, manifest.sha256, source_size
            )
        except ObjectStoreNotFoundError as exc:
            db.rollback()
            raise ResourceNotFoundError("Recording was not found") from exc
        except OperationalError as exc:
            db.rollback()
            raise RecordingPlaybackError("Recording playback is busy") from exc
        except Exception:
            db.rollback()
            raise
        stream_size = int(playback_metadata["ContentLength"])
        stream_etag = f'"{playback_metadata["Metadata"]["sha256"]}"'
    if range_header is not None and if_range is not None and if_range.strip() != stream_etag:
        range_header = None
    if full_download:
        range_header = None
    try:
        byte_range = parse_single_byte_range(range_header, stream_size)
    except ValueError:
        if playback_db_lock:
            db.rollback()
        return Response(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Range": f"bytes */{stream_size}",
                "Cache-Control": "private, max-age=0, must-revalidate",
            },
        )
    try:
        stored = store.get_object_stream(stream_key, byte_range)
    except ObjectStoreNotFoundError as exc:
        if playback_db_lock:
            db.rollback()
        raise ResourceNotFoundError("Recording was not found") from exc
    except Exception:
        if playback_db_lock:
            db.rollback()
        raise
    if playback_db_lock:
        try:
            db.commit()
        except Exception:
            stored["Body"].close()
            db.rollback()
            raise
    body = stored["Body"]
    content_length = stream_size if full_download or byte_range is None else byte_range[1] - byte_range[0] + 1
    if content_type == "application/octet-stream":
        content_type = "audio/webm"
    extension = {
        "audio/aac": "aac",
        "audio/mpeg": "mp3",
        "audio/mp4": "m4a",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/webm": "webm",
        "audio/x-wav": "wav",
    }.get(content_type, "audio")
    download_filename = recording_download_filename(conversation.occurred_at, conversation.name, extension)
    disposition = f'attachment; filename="{download_filename}"' if download else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
        "ETag": stream_etag,
        "Content-Disposition": disposition,
        "Content-Length": str(content_length),
        "X-Content-Type-Options": "nosniff",
    }
    response_status = status.HTTP_200_OK
    if byte_range and not full_download:
        headers["Content-Range"] = f"bytes {byte_range[0]}-{byte_range[1]}/{stream_size}"
        response_status = status.HTTP_206_PARTIAL_CONTENT
    return StreamingResponse(
        stream_object_body(body),
        status_code=response_status,
        media_type=content_type,
        headers=headers,
        background=BackgroundTask(body.close),
    )


@router.patch("/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: UUID,
    payload: ConversationUpdate,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
):
    """
    Apply an optimistic-concurrency metadata update.

    - Authenticate the caller, validate the request payload, and fingerprint and reserve the
      idempotency key.
    - On replay, return the response stored with the completed idempotency record.
    - Load the caller's active conversation and validate the expected revision.
    - Update the explicitly supplied metadata fields and increment the conversation revision.
    - Flush the database update, build the response from current artifact manifests, and
      complete the idempotency record with the response.
    - Commit the database transaction and return the updated conversation.
    """
    require_capability("metadata:write", caller)
    fingerprint = request_fingerprint({"conversation_id": str(conversation_id), **payload.model_dump(mode="json")})
    with IdempotencyContext(db, caller.subject, "conversation_update", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            replay = idempotency.response_data
            return ResourceNotFoundError.enforce_defined(replay, "Conversation response was not found")

        item = owned_conversation(conversation_id, caller, db)
        RevisionConflictError.require_condition(
            payload.expected_revision == item.revision,
            "Conversation revision conflict",
        )
        for key, value in payload.model_dump(exclude={"expected_revision"}, exclude_unset=True).items():
            setattr(item, key, value)
        item.revision += 1
        db.flush()
        response = conversation_response(item, db)
        idempotency.capture(item.id, response.model_dump(mode="json"))
    db.commit()
    return response


@router.delete("/{conversation_id}", status_code=202)
def delete_conversation(
    conversation_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
):
    """
    Tombstone a whole conversation and remove its governed content.

    - Authenticate the caller, fingerprint, and reserve the idempotency key.
    - On replay, return the stored deletion response, or reconstruct the standard response
      when the completed record has no response data.
    - Load the caller's active conversation and validate ownership and tombstone state.
    - Insert a conversation tombstone, delete all governed database records, and delete the
      conversation row.
    - Complete the idempotency record with the deletion response and commit the database transaction.
    - Delete the conversation's objects from the object store after the database commit.

    Return an accepted deletion response after database and object-store cleanup succeeds.
    """
    require_capability("deletion:write", caller)
    fingerprint = request_fingerprint({"conversation_id": str(conversation_id)})
    with IdempotencyContext(db, caller.subject, "conversation_delete", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            if not idempotency.response_data:
                return dict(kind=ErrorKind.CONVERSATION_DELETED.value, conversation_id=str(conversation_id))
            return idempotency.response_data

        item = owned_conversation(conversation_id, caller, db, for_update=True)
        db.add(Tombstone(conversation_id=item.id))
        for model in (Note, ArtifactManifest, ProcessingAttempt, ConversationSpeaker, OutboxMessage, Job, UploadSession, AuditEvent, Operation):
            db.query(model).filter_by(conversation_id=conversation_id).delete(synchronize_session=False)
        db.delete(item)
        response = {"kind": ErrorKind.CONVERSATION_DELETED.value, "conversation_id": str(conversation_id)}
        idempotency.capture(conversation_id, response)
    db.commit()

    with ConversationCleanupError.handle_errors("Conversation object cleanup failed"):
        store.delete_conversation(str(conversation_id))

    return response


@router.post("/{conversation_id}/recording", status_code=201)
async def upload_recording(
    conversation_id: UUID,
    idempotency_key: IdempotencyKeyHeader,
    recording: UploadFile = File(...),
    content_sha256: str = Header(..., alias="X-Content-SHA256"),
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
    store: ObjectStore = Depends(get_object_store),
) -> dict[str, object]:
    """
    Store and verify a bounded source recording before creating processing state.

    - Authenticate the caller and reserve the idempotency key.
    - Load the caller's active conversation and validate the recording type and size.
    - Read the upload into a temporary file while calculating its SHA-256 digest.
    - Compare the calculated digest with the client-supplied digest.
    - Store the verified recording in object storage.
    - Insert an `Operation` with the committed upload status.
    - Insert a committed `UploadSession` recording the verified size and digest.
    - Insert the current recording `ArtifactManifest`.
    - Mark the conversation as processing.
    - Insert the transcription `Job` and its `OutboxMessage`.
    - Complete the idempotency record and commit all database changes.

    Return the operation identifier, conversation identifier, digest, size, and status.
    """
    require_capability("metadata:write", caller)
    recording_media_type = (recording.content_type or "").partition(";")[0].strip().lower()
    fingerprint = request_fingerprint(
        {"conversation_id": str(conversation_id), "content_type": recording_media_type, "content_sha256": content_sha256.lower(), "content_length": recording.size}
    )
    with RecordingUploadError.handle_errors(
        "Recording upload failed",
        ignore_exc_class=MuninnError,
    ):
        with IdempotencyContext(db, caller.subject, "recording_upload", idempotency_key, fingerprint) as idempotency:
            if idempotency.is_replay:
                if not idempotency.response_data:
                    return dict(
                        operation_id=str(idempotency.resource_id),
                        conversation_id=str(conversation_id),
                        status=OperationStatus.COMMITTED.value
                    )
                return idempotency.response_data

            UnsupportedMediaError.require_condition(
                recording_media_type in ALLOWED_RECORDING_TYPES,
                "Unsupported recording media type",
            )

            item = owned_conversation(conversation_id, caller, db)

            with SpooledTemporaryFile(max_size=MAX_RECORDING_SPOOL_BYTES, mode="w+b") as temporary:
                digest = sha256()
                size = 0
                while chunk := await recording.read(RECORDING_READ_CHUNK_BYTES):
                    size += len(chunk)
                    MediaTooLargeError.require_condition(size <= MAX_RECORDING_BYTES, "Recording is too large")
                    digest.update(chunk)
                    temporary.write(chunk)
                temporary.seek(0)
                digest = digest.hexdigest()
                MediaDigestMismatchError.require_condition(
                    content_sha256.lower() == digest,
                    "Recording digest does not match content",
                )
                object_key = f"conversations/{conversation_id}/recording/{digest}"
                temporary.seek(0)
                store.put_stream(
                    object_key,
                    cast(BinaryIO, temporary),
                    size,
                    ArtifactKind.RECORDING,
                    recording_media_type,
                )
                operation = Operation(
                    conversation_id=conversation_id,
                    idempotency_key=str(idempotency_key),
                    kind=OperationKind.RECORDING_UPLOAD,
                )
                db.add(operation)
                db.flush()
                db.add(UploadSession(conversation_id=conversation_id, operation_id=operation.id, expected_size=size, expected_sha256=digest, acknowledged_bytes=size, state=UploadState.COMMITTED))
                db.add(ArtifactManifest(conversation_id=conversation_id, kind=ArtifactKind.RECORDING, version=1, object_key=object_key, sha256=digest, byte_size=size, source_version=1, is_current=True))
                item.sync_state = SyncState.PROCESSING
                job = Job(conversation_id=conversation_id, kind=ProcessingKind.TRANSCRIPTION, source_version=1)
                db.add(job)
                db.flush()
                outbox_payload = OutboxJobPayload(
                    job_id=job.id,
                    conversation_id=conversation_id,
                    kind=ProcessingKind.TRANSCRIPTION,
                    source_version=1,
                    source_key=object_key,
                )
                db.add(OutboxMessage(conversation_id=conversation_id, subject=NatsSubject.JOBS, payload=outbox_payload.model_dump(mode="json", exclude_none=True), deduplication_key=TRANSCRIPTION_OUTBOX_DEDUPLICATION_KEY.format(conversation_id=conversation_id, job_id=job.id)))
                operation.status = OperationStatus.COMMITTED
                response = {
                    "operation_id": str(operation.id),
                    "conversation_id": str(conversation_id),
                    "sha256": digest,
                    "size": size,
                    "status": operation.status.value,
                }
                idempotency.capture(operation.id, response)
        db.commit()
    return response


@router.post("/{conversation_id}/notes", response_model=NoteResponse, status_code=201)
def create_note(
    conversation_id: UUID,
    payload: NoteCreate,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
):
    """
    Create one human timestamped note after validating ownership and offset.

    - Authenticate the caller and fingerprint the conversation and note payload.
    - Load the caller's active conversation and validate that the note offset is within the
      recording duration when one is available.
    - Reserve the caller-scoped idempotency key; on replay, load and return the recorded note,
      or raise a not-found error if it no longer exists.
    - Insert a human-authored note, flush it to obtain its ID, and complete the idempotency
      record with that ID.
    - Commit the database transaction, refresh the note, and return its response.
    """
    require_capability("notes:human:create", caller)
    fingerprint = request_fingerprint({"conversation_id": str(conversation_id), **payload.model_dump(mode="json")})
    item = owned_conversation(conversation_id, caller, db)
    NoteOffsetOutOfRangeError.require_condition(
        item.recording_duration_seconds is None or payload.offset_seconds <= item.recording_duration_seconds,
        "Note offset is outside the recording",
    )
    with IdempotencyContext(db, caller.subject, "human_note", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            note = db.get(Note, idempotency.resource_id)
            return NoteResponse.model_validate(note) if note else ResourceNotFoundError.enforce_defined(note, "Note was not found")
        note = Note(conversation_id=item.id, subject=caller.subject, author_kind=AuthorKind.HUMAN, **payload.model_dump())
        db.add(note)
        db.flush()
        idempotency.capture(note.id)
    db.commit()
    db.refresh(note)
    return NoteResponse.model_validate(note)


@router.get("/{conversation_id}/notes", response_model=list[NoteResponse])
def list_notes(conversation_id: UUID, caller: Principal = Depends(principal), db: Session = Depends(db_session)):
    """List owned notes in recording order."""
    require_capability("context:read", caller)
    owned_conversation(conversation_id, caller, db)
    notes = db.query(Note).filter_by(conversation_id=conversation_id, deleted_at=None).order_by(Note.offset_seconds).limit(50).all()
    return [NoteResponse.model_validate(note) for note in notes]


@router.get("/{conversation_id}/notes/export")
def export_notes(
    conversation_id: UUID,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
) -> StreamingResponse:
    """Download all active notes as a JSON array using the public note schema."""
    require_capability("context:read", caller)
    owned_conversation(conversation_id, caller, db)
    export_sessions = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    return StreamingResponse(
        stream_notes_json(export_sessions, conversation_id),
        media_type="application/json",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="notes.json"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.patch("/{conversation_id}/notes/{note_id}", response_model=NoteResponse)
def update_note(
    conversation_id: UUID,
    note_id: UUID,
    payload: NoteUpdate,
    idempotency_key: IdempotencyKeyHeader,
    caller: Principal = Depends(principal),
    db: Session = Depends(db_session),
):
    """
    Update only the text of an owned human note.

    Preserve the note timestamp, authorship, and creation time while replaying an identical request safely.
    """
    require_capability("notes:human:edit", caller)
    owned_conversation(conversation_id, caller, db)
    fingerprint = request_fingerprint(
        {"conversation_id": str(conversation_id), "note_id": str(note_id), "text": payload.text}
    )
    with IdempotencyContext(db, caller.subject, "human_note_update", idempotency_key, fingerprint) as idempotency:
        if idempotency.is_replay:
            replay = ResourceNotFoundError.enforce_defined(idempotency.response_data, "Note response was not found")
            return NoteResponse.model_validate(replay)
        note = db.query(Note).filter_by(id=note_id, conversation_id=conversation_id, deleted_at=None).first()
        note = ResourceNotFoundError.enforce_defined(note, "Note was not found")
        ResourceNotFoundError.require_condition(
            note.subject == caller.subject and note.author_kind == AuthorKind.HUMAN,
            "Note was not found",
        )
        note.text = payload.text
        db.flush()
        response = NoteResponse.model_validate(note)
        idempotency.capture(note.id, response.model_dump(mode="json"))
    db.commit()
    return response


@router.delete("/{conversation_id}/notes/{note_id}", status_code=204)
def delete_note(
conversation_id: UUID, note_id: UUID, caller: Principal = Depends(principal), db: Session = Depends(db_session)):
    """
    Delete only an owned human note.

    - Authenticate the caller and load the caller's active conversation.
    - Validate that the note belongs to the conversation and caller and was authored by a human.
    - Delete the note row, commit the database transaction, and return a no-content response.
    """
    require_capability("notes:human:delete", caller)
    owned_conversation(conversation_id, caller, db)
    note = db.query(Note).filter_by(id=note_id, conversation_id=conversation_id).first()
    note = ResourceNotFoundError.enforce_defined(note, "Note was not found")
    ResourceNotFoundError.require_condition(
        note.subject == caller.subject and note.author_kind == AuthorKind.HUMAN,
        "Note was not found",
    )
    db.delete(note)
    db.commit()
    return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
