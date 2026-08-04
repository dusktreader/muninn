"""Verify that completion correlation prevents stale results from being promoted."""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, cast
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from anyio import run
from sqlalchemy import create_engine, event as sqlalchemy_event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState, SyncState, VisibilityState
from muninn.job_manager import CompletionRejectedError, JobManager
from muninn.db.models import ArtifactManifest, Base, Conversation, ConversationSpeaker, Job, OutboxMessage, ProcessingAttempt, Tombstone
from muninn.queue.contracts import CompletionEvent, ResultObject
from muninn.queue.exceptions import ObjectStoreUnavailableError
from muninn.queue.manager import QueueManager
from muninn.schemas import SummaryItem, SummaryResult, TranscriptResult, TranscriptSegment
from muninn.storage import ObjectStore, StoredConversationArtifact


class UnusedObjectStore:
    """Fail if a rejected event tries to read or promote its result."""

    async def get(self, _: ResultObject) -> bytes:
        raise AssertionError("rejected completion must not read its result")


class UnusedCanonicalStore:
    """Fail if a rejected event tries to write a canonical artifact."""

    def get_bytes(self, _: str) -> bytes:
        return _transcript_content()

    def put_bytes(self, *_: object) -> None:
        raise AssertionError("rejected completion must not promote its result")


class CompletionObjectStore:
    """Return a fixed successful completion payload."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def get(self, _: ResultObject) -> bytes:
        return self.content


class FailingObjectStore:
    """Raise before canonical storage is attempted."""

    async def get(self, _: ResultObject) -> bytes:
        raise RuntimeError("result object unavailable")


class CompletionCanonicalStore:
    """Return canonical metadata for a successful completion."""

    def __init__(self, artifact: StoredConversationArtifact) -> None:
        self.artifact = artifact

    def get_bytes(self, _: str) -> bytes:
        return _transcript_content()

    def put_conversation_artifact(self, *_: object) -> StoredConversationArtifact:
        return self.artifact


class FailingCanonicalStore:
    """Raise when canonical artifact storage is attempted."""

    def get_bytes(self, _: str) -> bytes:
        return _transcript_content()

    def put_conversation_artifact(self, *_: object) -> StoredConversationArtifact:
        raise RuntimeError("canonical store unavailable")


class UnusedCanonicalInputStore:
    """Fail if malformed outbox data reaches canonical object storage."""

    def get_bytes(self, _: str) -> bytes:
        raise AssertionError("malformed outbox payload must not read its source")


@contextmanager
def _session_context(sessions: Any, in_transaction: bool = False) -> Iterator[Session]:
    """Provide the production session-factory contract for test databases."""
    with sessions() as db:
        if in_transaction:
            with db.begin():
                yield db
        else:
            yield db


def _session_factory(sessions: Any) -> Any:
    """Bind a test sessionmaker to the production session-factory contract."""
    return lambda in_transaction=False: _session_context(sessions, in_transaction)


def _manager(sessions: Any, result_store: object, canonical_store: object) -> JobManager:
    """Build a manager with test doubles at the queue and canonical-store boundaries."""
    return JobManager(
        queue=cast(QueueManager, Mock(object_store=result_store)),
        canonical_store=cast(ObjectStore, canonical_store),
        session_factory=_session_factory(sessions),
    )


def _summary_content(text: str) -> bytes:
    """Serialize one schema-valid summary claim for completion tests."""
    summary = SummaryResult(
        overview=SummaryItem(text=text, evidence_span_ids=["segment-1"]),
        provider="fake",
        model="fake-summary",
    )
    return summary.model_dump_json().encode()


def _transcript_content() -> bytes:
    segment = TranscriptSegment(
        evidence_id="segment-1",
        speaker="Speaker 1",
        start_seconds=0,
        end_seconds=1,
        text="A source transcript claim.",
    )
    return TranscriptResult(segments=[segment], provider="fake", model="fake-transcription").model_dump_json().encode()


@pytest.mark.unit
def test_dispatch_outbox_logs_and_skips_malformed_payload_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log malformed outbox data and do not publish a job for the invalid row."""
    run(_test_dispatch_outbox_logs_and_skips_malformed_payload_without_publishing, monkeypatch)


async def _test_dispatch_outbox_logs_and_skips_malformed_payload_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        outbox = OutboxMessage(
            conversation_id=uuid4(),
            subject="muninn.jobs",
            payload={"kind": ProcessingKind.TRANSCRIPTION.value, "source_version": 1, "source_key": "recording.wav"},
            deduplication_key="malformed-outbox",
        )
        db.add(outbox)

    queue = Mock(object_store=Mock())
    queue.publish_job = Mock()
    manager = JobManager(
        queue=cast(QueueManager, queue),
        canonical_store=cast(ObjectStore, UnusedCanonicalInputStore()),
        session_factory=_session_factory(sessions),
    )
    logged: list[tuple[BaseException, str, dict[str, object]]] = []
    monkeypatch.setattr(
        "muninn.job_manager.service.log_error",
        lambda exc, message, *, context=None: logged.append((exc, message, context or {})),
    )

    assert await manager.dispatch_outbox_once() == 0
    assert len(logged) == 1
    exception, log_message, context = logged[0]
    assert isinstance(exception, ValueError)
    assert str(exception)
    assert log_message == "Outbox dispatch failed while preparing a job; dispatch will retry/skip this row"
    assert context == {"outbox_id": outbox.id, "conversation_id": outbox.conversation_id}
    assert "recording.wav" not in log_message
    queue.publish_job.assert_not_called()
    queue.object_store.put.assert_not_called()
    engine.dispose()


@pytest.mark.unit
def test_outbox_dispatch_deletes_object_after_ambiguous_put_failure() -> None:
    run(_test_outbox_dispatch_deletes_object_after_ambiguous_put_failure)


async def _test_outbox_dispatch_deletes_object_after_ambiguous_put_failure() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(Conversation(id=conversation_id, owner_subject="owner", occurred_at=datetime.now(timezone.utc)))
        db.add(
            Job(
                id=job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.TRANSCRIPTION,
                source_version=1,
            )
        )
        outbox = OutboxMessage(
            conversation_id=conversation_id,
            subject="muninn.jobs",
            payload={
                "job_id": str(job_id),
                "conversation_id": str(conversation_id),
                "kind": ProcessingKind.TRANSCRIPTION.value,
                "source_version": 1,
                "source_key": "recordings/source.wav",
            },
            deduplication_key="ambiguous-put",
        )
        db.add(outbox)

    objects: dict[str, bytes] = {}

    async def put_then_fail(key: str, content: bytes, _: ArtifactKind) -> ResultObject:
        objects[key] = content
        raise ObjectStoreUnavailableError("put response was lost")

    async def delete_key(key: str) -> None:
        objects.pop(key, None)

    class CanonicalInputStore:
        def get_bytes(self, _: str) -> bytes:
            return b"recording bytes"

    queue = Mock(object_store=Mock())
    queue.object_store.put = AsyncMock(side_effect=put_then_fail)
    queue.object_store.delete_key = AsyncMock(side_effect=delete_key)
    queue.publish_job = AsyncMock()
    manager = JobManager(
        queue=cast(QueueManager, queue),
        canonical_store=cast(ObjectStore, CanonicalInputStore()),
        session_factory=_session_factory(sessions),
    )

    assert await manager.dispatch_outbox_once() == 0
    queue.object_store.delete_key.assert_awaited_once()
    assert objects == {}
    queue.publish_job.assert_not_awaited()
    with sessions() as db:
        stored_outbox = db.get(OutboxMessage, outbox.id)
        stored_job = db.get(Job, job_id)
        assert stored_outbox is not None
        assert stored_outbox.published_at is None
        assert stored_job is not None
        assert stored_job.state is ProcessingState.QUEUED
    engine.dispose()


@pytest.mark.unit
def test_outbox_dispatch_preserves_mapping_snapshot_in_worker_message() -> None:
    """Carry the persisted speaker mapping and revision into the published worker job."""
    run(_test_outbox_dispatch_preserves_mapping_snapshot_in_worker_message)


async def _test_outbox_dispatch_preserves_mapping_snapshot_in_worker_message() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_subject="owner",
                occurred_at=datetime.now(timezone.utc),
                speaker_revision=5,
            )
        )
        db.add_all(
            [
                Job(
                    id=job_id,
                    conversation_id=conversation_id,
                    kind=ProcessingKind.SUMMARIZATION,
                    source_version=1,
                    transcript_version=2,
                    speaker_revision=5,
                    speaker_mapping={"Speaker 1": "Ada Lovelace"},
                ),
                ArtifactManifest(
                    conversation_id=conversation_id,
                    kind=ArtifactKind.TRANSCRIPT,
                    version=2,
                    object_key="transcripts/current.json",
                    sha256="a" * 64,
                    byte_size=15,
                    source_version=1,
                    is_current=True,
                ),
                OutboxMessage(
                    conversation_id=conversation_id,
                    subject="muninn.jobs",
                    payload={
                        "job_id": str(job_id),
                        "conversation_id": str(conversation_id),
                        "kind": ProcessingKind.SUMMARIZATION.value,
                        "source_version": 1,
                        "transcript_version": 2,
                        "speaker_revision": 5,
                        "speaker_mapping": {"Speaker 1": "Ada Lovelace"},
                        "source_key": "transcripts/current.json",
                    },
                    deduplication_key="speaker-mapping-job",
                ),
            ]
        )

    class CanonicalInputStore:
        def get_bytes(self, _: str) -> bytes:
            return b'{"segments":[]}'

    reference = ResultObject(
        kind=ArtifactKind.TRANSCRIPT,
        bucket="inputs",
        key="transcripts/current.json",
        sha256="a" * 64,
        byte_size=15,
    )
    queue = Mock(object_store=Mock())
    queue.object_store.put = AsyncMock(return_value=reference)
    queue.publish_job = AsyncMock()
    manager = JobManager(
        queue=cast(QueueManager, queue),
        canonical_store=cast(ObjectStore, CanonicalInputStore()),
        session_factory=_session_factory(sessions),
    )

    assert await manager.dispatch_outbox_once() == 1
    published_call = queue.publish_job.await_args
    assert published_call is not None
    message = published_call.args[0]
    assert message.job_id == job_id
    assert message.speaker_revision == 5
    assert message.speaker_mapping == {"Speaker 1": "Ada Lovelace"}

    stale_job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Job(
                id=stale_job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=1,
                transcript_version=2,
                speaker_revision=4,
            )
        )
        db.add(
            OutboxMessage(
                conversation_id=conversation_id,
                subject="muninn.jobs",
                payload={
                    "job_id": str(stale_job_id),
                    "conversation_id": str(conversation_id),
                    "kind": ProcessingKind.SUMMARIZATION.value,
                    "source_version": 1,
                    "transcript_version": 2,
                    "speaker_revision": 4,
                    "speaker_mapping": {},
                    "source_key": "transcripts/current.json",
                },
                deduplication_key="stale-speaker-mapping-job",
            )
        )

    assert await manager.dispatch_outbox_once() == 0
    assert queue.publish_job.await_count == 1
    assert queue.object_store.put.await_count == 1
    with sessions() as db:
        stale_job = db.get(Job, stale_job_id)
        stale_outbox = db.query(OutboxMessage).filter_by(deduplication_key="stale-speaker-mapping-job").one()
        assert stale_job is not None
        assert stale_job.state is ProcessingState.CANCELLED
        assert stale_outbox.published_at is not None
        assert stale_outbox.attempts == 0

    tombstoned_job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Job(
                id=tombstoned_job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.TRANSCRIPTION,
                source_version=2,
            )
        )
        db.add(
            OutboxMessage(
                conversation_id=conversation_id,
                subject="muninn.jobs",
                payload={
                    "job_id": str(tombstoned_job_id),
                    "conversation_id": str(conversation_id),
                    "kind": ProcessingKind.TRANSCRIPTION.value,
                    "source_version": 2,
                    "speaker_revision": 0,
                    "speaker_mapping": {},
                    "source_key": "recordings/current.wav",
                },
                deduplication_key="tombstoned-recording-job",
            )
        )
    tombstoned_reference = ResultObject(
        kind=ArtifactKind.RECORDING,
        bucket="inputs",
        key="inputs/tombstoned-copy",
        sha256="b" * 64,
        byte_size=10,
    )

    async def put_then_tombstone(key: str, __: bytes, ___: ArtifactKind) -> ResultObject:
        with sessions.begin() as db:
            db.add(Tombstone(conversation_id=conversation_id))
        return tombstoned_reference.model_copy(update={"key": key})

    queue.object_store.put.side_effect = put_then_tombstone
    queue.object_store.delete_key = AsyncMock()
    assert await manager.dispatch_outbox_once() == 0
    put_call = queue.object_store.put.await_args
    assert put_call is not None
    queue.object_store.delete_key.assert_awaited_once_with(put_call.args[0])
    assert queue.publish_job.await_count == 1
    with sessions() as db:
        tombstoned_job = db.get(Job, tombstoned_job_id)
        tombstoned_outbox = db.query(OutboxMessage).filter_by(deduplication_key="tombstoned-recording-job").one()
        assert tombstoned_job is not None
        assert tombstoned_job.state is ProcessingState.CANCELLED
        assert tombstoned_outbox.published_at is not None
    engine.dispose()


def _seed_summary_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, UUID, UUID, CompletionEvent, bytes]:
    """Create a database and successful summary completion for failure-path tests."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_subject="owner",
                occurred_at=datetime.now(timezone.utc),
                sync_state=SyncState.PROCESSING,
                visibility=VisibilityState.ACTIVE,
            )
        )
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=1,
                object_key="transcript-v1.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            )
        )
        db.add(
            Job(
                id=job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=1,
                transcript_version=1,
                state=ProcessingState.QUEUED,
            )
        )
    content = _summary_content("new")
    event = CompletionEvent(
        job_id=job_id,
        conversation_id=conversation_id,
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        state=ProcessingState.SUCCEEDED,
        result_object=ResultObject(
            kind=ArtifactKind.SUMMARY,
            bucket="results",
            key="result.json",
            sha256="c" * 64,
            byte_size=len(content),
        ),
    )
    return engine, sessions, conversation_id, job_id, event, content


@pytest.mark.unit
def test_apply_completion_logs_and_propagates_canonical_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log canonical storage failures without changing their exception semantics."""
    run(_test_apply_completion_logs_and_propagates_canonical_store_failure, monkeypatch)


async def _test_apply_completion_logs_and_propagates_canonical_store_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    manager = _manager(sessions, CompletionObjectStore(_summary_content("result")), FailingCanonicalStore())
    logged: list[tuple[BaseException, str]] = []
    monkeypatch.setattr("muninn.job_manager.service.log_error", lambda exc, message: logged.append((exc, message)))

    with pytest.raises(RuntimeError, match="canonical store unavailable") as raised:
        await manager.apply_completion(completion)

    assert len(logged) == 1
    assert logged[0][0] is raised.value
    assert str(job_id) in logged[0][1]
    assert str(conversation_id) in logged[0][1]
    assert "completion_kind=summarization" in logged[0][1]
    with sessions() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.state is ProcessingState.QUEUED
        assert db.get(Conversation, conversation_id).sync_state is SyncState.PROCESSING
        assert db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all() == []
    engine.dispose()


@pytest.mark.unit
def test_apply_completion_rolls_back_when_result_object_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave database state unchanged when object-store I/O fails in the transaction."""
    run(_test_apply_completion_rolls_back_when_result_object_read_fails, monkeypatch)


async def _test_apply_completion_rolls_back_when_result_object_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    manager = _manager(sessions, FailingObjectStore(), UnusedCanonicalStore())

    with pytest.raises(RuntimeError, match="result object unavailable"):
        await manager.apply_completion(completion)

    with sessions() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.state is ProcessingState.QUEUED
        assert db.get(Job, job_id).attempts == 0
        assert db.get(Conversation, conversation_id).sync_state is SyncState.PROCESSING
        assert db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all() == []
    engine.dispose()


@pytest.mark.unit
def test_result_object_await_does_not_hold_sqlite_write_lock_and_rechecks_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(_test_result_object_await_does_not_hold_sqlite_write_lock_and_rechecks_lineage, monkeypatch)


async def _test_result_object_await_does_not_hold_sqlite_write_lock_and_rechecks_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, content = _seed_summary_completion(monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingObjectStore:
        async def get(self, _: ResultObject) -> bytes:
            started.set()
            await release.wait()
            return content

    manager = _manager(sessions, BlockingObjectStore(), UnusedCanonicalStore())
    applying = asyncio.create_task(manager.apply_completion(completion))
    await started.wait()

    with sessions.begin() as db:
        db.get(Conversation, conversation_id).speaker_revision = 1
    release.set()

    with pytest.raises(CompletionRejectedError, match="speaker mapping is no longer current"):
        await applying

    with sessions() as db:
        assert db.get(Job, job_id).state is ProcessingState.QUEUED
        assert db.query(ArtifactManifest).filter_by(kind=ArtifactKind.SUMMARY).count() == 0
    engine.dispose()


@pytest.mark.unit
def test_apply_running_event_marks_the_job_running_without_promoting_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mark a queued job running without changing conversation or artifact state."""
    run(_test_apply_running_event_marks_the_job_running, monkeypatch)


async def _test_apply_running_event_marks_the_job_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())
    running_event = CompletionEvent(
        job_id=completion.job_id,
        conversation_id=completion.conversation_id,
        kind=completion.kind,
        source_version=completion.source_version,
        transcript_version=completion.transcript_version,
        state=ProcessingState.RUNNING,
    )

    await manager.apply_completion(running_event)

    with sessions() as db:
        job = db.get(Job, job_id)
        conversation = db.get(Conversation, conversation_id)
        assert job.state is ProcessingState.RUNNING
        assert job.attempts == 0
        attempt = db.scalar(select(ProcessingAttempt).where(ProcessingAttempt.job_id == job_id))
        assert attempt is not None
        assert attempt.attempt_number == 1
        assert attempt.state is ProcessingState.RUNNING
        assert conversation.sync_state is SyncState.PROCESSING
        assert db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all() == []
    engine.dispose()


@pytest.mark.unit
def test_apply_completion_persists_failed_job_and_conversation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persist a failed completion and atomically increment its attempt count."""
    run(_test_apply_completion_persists_failed_job_and_conversation_state, monkeypatch)


async def _test_apply_completion_persists_failed_job_and_conversation_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="prior-summary.json",
                sha256="b" * 64,
                byte_size=10,
                source_version=1,
                transcript_version=1,
                is_current=True,
            )
        )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())
    failed_completion = completion.model_copy(
        update={
            "state": ProcessingState.FAILED,
            "result_object": None,
            "error_code": ProcessingErrorCode.INVALID_INPUT,
            "error_details": "provider rejected the input",
            "emitted_at": datetime.now(timezone.utc) + timedelta(seconds=1),
        }
    )

    await manager.apply_completion(failed_completion)

    with sessions() as db:
        job = db.get(Job, job_id)
        conversation = db.get(Conversation, conversation_id)
        assert job.state is ProcessingState.FAILED
        assert job.attempts == 1
        attempt = db.scalar(select(ProcessingAttempt).where(ProcessingAttempt.job_id == job_id))
        assert attempt is not None
        assert attempt.state is ProcessingState.FAILED
        assert attempt.error_code is ProcessingErrorCode.INVALID_INPUT
        assert attempt.error_details == "provider rejected the input"
        assert conversation.sync_state is SyncState.FAILED
        current_summary = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.SUMMARY,
                ArtifactManifest.is_current.is_(True),
            )
        )
        assert current_summary is not None
        assert current_summary.object_key == "prior-summary.json"
    engine.dispose()


@pytest.mark.unit
def test_apply_completion_logs_and_propagates_final_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log final persistence failures and leave the transaction rolled back."""
    run(_test_apply_completion_logs_and_propagates_final_database_failure, monkeypatch)


async def _test_apply_completion_logs_and_propagates_final_database_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, content = _seed_summary_completion(monkeypatch)
    manager = _manager(sessions, CompletionObjectStore(content), CompletionCanonicalStore(StoredConversationArtifact(ArtifactKind.SUMMARY, "summary.json", "c" * 64)))
    logged: list[tuple[BaseException, str]] = []
    monkeypatch.setattr("muninn.job_manager.service.log_error", lambda exc, message: logged.append((exc, message)))

    def fail_before_insert(_: Any, __: Any, ___: ArtifactManifest) -> None:
        raise RuntimeError("database unavailable")

    sqlalchemy_event.listen(ArtifactManifest, "before_insert", fail_before_insert)
    try:
        artifact = StoredConversationArtifact(ArtifactKind.SUMMARY, "summary.json", "c" * 64)
        with pytest.raises(RuntimeError, match="database unavailable") as raised:
            manager.canonical_store = cast(ObjectStore, CompletionCanonicalStore(artifact))
            await manager.apply_completion(completion)
    finally:
        sqlalchemy_event.remove(ArtifactManifest, "before_insert", fail_before_insert)

    assert len(logged) == 1
    assert logged[0][0] is raised.value
    assert str(job_id) in logged[0][1]
    assert str(conversation_id) in logged[0][1]
    assert "kind=summary" in logged[0][1]
    assert "version=1" in logged[0][1]
    with sessions() as db:
        assert db.get(Job, job_id).state is ProcessingState.QUEUED
        assert db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all() == []
    engine.dispose()


@pytest.mark.unit
def test_apply_completion_rejects_kind_source_and_transcript_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject every correlation dimension before touching the result object."""
    run(_test_apply_completion_rejects_mismatches, monkeypatch)


async def _test_apply_completion_rejects_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_subject="owner",
                occurred_at=datetime.now(timezone.utc),
                sync_state=SyncState.PROCESSING,
                visibility=VisibilityState.ACTIVE,
            )
        )
        db.add(
            Job(
                id=job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=2,
                transcript_version=3,
                state=ProcessingState.QUEUED,
            )
        )

    result = ResultObject(
        kind=ArtifactKind.SUMMARY,
        bucket="results",
        key="result.json",
        sha256="a" * 64,
        byte_size=1,
    )
    mismatches = (
        CompletionEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            kind=ProcessingKind.TRANSCRIPTION,
            source_version=2,
            transcript_version=None,
            state=ProcessingState.SUCCEEDED,
            result_object=ResultObject(
                kind=ArtifactKind.TRANSCRIPT,
                bucket=result.bucket,
                key=result.key,
                sha256=result.sha256,
                byte_size=result.byte_size,
            ),
        ),
        CompletionEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            kind=ProcessingKind.SUMMARIZATION,
            source_version=1,
            transcript_version=3,
            state=ProcessingState.SUCCEEDED,
            result_object=result,
        ),
        CompletionEvent(
            job_id=job_id,
            conversation_id=conversation_id,
            kind=ProcessingKind.SUMMARIZATION,
            source_version=2,
            transcript_version=4,
            state=ProcessingState.SUCCEEDED,
            result_object=result,
        ),
    )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())
    for event in mismatches:
        with pytest.raises(CompletionRejectedError) as rejected:
            await manager.apply_completion(event)
        message = str(rejected.value)
        assert str(job_id) in message
        assert str(conversation_id) in message
        assert f"event_kind={event.kind.value}" in message
        assert f"job_kind={ProcessingKind.SUMMARIZATION.value}" in message
        assert f"event_source_version={event.source_version}" in message
        assert "job_source_version=2" in message
        assert f"event_transcript_version={event.transcript_version}" in message
        assert "job_transcript_version=3" in message
        assert "result.json" not in message
        assert "a" * 64 not in message

    with sessions() as db:
        job = db.get(Job, job_id)
        assert job is not None
        assert job.state is ProcessingState.QUEUED
        assert db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all() == []
    engine.dispose()


@pytest.mark.unit
def test_apply_completion_replaces_existing_current_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the newly applied artifact current and clear the prior current manifest."""
    run(_test_apply_completion_replaces_existing_current_manifest, monkeypatch)


async def _test_apply_completion_replaces_existing_current_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_subject="owner",
                occurred_at=datetime.now(timezone.utc),
                sync_state=SyncState.PROCESSING,
                visibility=VisibilityState.ACTIVE,
            )
        )
        db.add(
            Job(
                id=job_id,
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=1,
                transcript_version=1,
                state=ProcessingState.QUEUED,
            )
        )
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=1,
                object_key="transcript-v1.json",
                sha256="a" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            )
        )
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="old-summary.json",
                sha256="b" * 64,
                byte_size=3,
                source_version=1,
                is_current=True,
            )
        )

    content = _summary_content("new")
    event = CompletionEvent(
        job_id=job_id,
        conversation_id=conversation_id,
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        state=ProcessingState.SUCCEEDED,
        result_object=ResultObject(
            kind=ArtifactKind.SUMMARY,
            bucket="results",
            key="result.json",
            sha256="c" * 64,
            byte_size=len(content),
        ),
    )
    new_artifact = StoredConversationArtifact(
        kind=ArtifactKind.SUMMARY,
        key="new-summary.json",
        sha256="c" * 64,
    )
    manager = _manager(sessions, CompletionObjectStore(content), CompletionCanonicalStore(new_artifact))

    await manager.apply_completion(event)

    with sessions() as db:
        manifests = db.scalars(
            select(ArtifactManifest)
            .where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
            .order_by(ArtifactManifest.version)
        ).all()
        assert [(manifest.version, manifest.is_current) for manifest in manifests] == [(1, False), (2, True)]
        assert manifests[0].object_key == "old-summary.json"
        assert manifests[1].object_key == "new-summary.json"
        assert manifests[1].transcript_version == 1
        job = db.get(Job, job_id)
        conversation = db.get(Conversation, conversation_id)
        assert job is not None
        assert conversation is not None
        assert job.state is ProcessingState.SUCCEEDED
        assert job.attempts == 1
        assert conversation.sync_state is SyncState.READY
    engine.dispose()


@pytest.mark.unit
def test_artifact_manifest_exposes_partial_current_index() -> None:
    """Expose one-current-manifest enforcement as a PostgreSQL partial unique index."""
    table = Base.metadata.tables[ArtifactManifest.__tablename__]
    current_index = next(index for index in table.indexes if index.name == "uq_artifact_manifests_current")
    assert current_index.unique is True
    assert [column.name for column in current_index.columns] == ["conversation_id", "kind"]
    assert str(current_index.dialect_options["postgresql"]["where"]) == "is_current"


@pytest.mark.unit
def test_stale_summary_completion_cannot_replace_current_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject results for a superseded transcript and keep its summary non-current."""
    run(_test_stale_summary_completion_cannot_replace_current_summary, monkeypatch)


async def _test_stale_summary_completion_cannot_replace_current_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, _, completion, _ = _seed_summary_completion(monkeypatch)
    old_summary = ArtifactManifest(
        conversation_id=conversation_id,
        kind=ArtifactKind.SUMMARY,
        version=1,
        object_key="old-summary.json",
        sha256="b" * 64,
        byte_size=10,
        source_version=1,
        transcript_version=1,
        is_current=True,
    )
    with sessions.begin() as db:
        db.add(old_summary)
        transcript = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
            )
        )
        transcript.is_current = False
        old_summary.is_current = False
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=2,
                object_key="transcript-v2.json",
                sha256="d" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            )
        )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    with pytest.raises(CompletionRejectedError, match="summary transcript is no longer current"):
        await manager.apply_completion(completion)

    with sessions() as db:
        current_summary = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.SUMMARY,
                ArtifactManifest.is_current.is_(True),
            )
        )
        assert current_summary is None
        old_summary = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.SUMMARY,
            )
        )
        assert old_summary is not None
        assert old_summary.object_key == "old-summary.json"
        assert old_summary.is_current is False
    engine.dispose()


@pytest.mark.unit
@pytest.mark.parametrize(
    "invalid_content",
    [
        b"PRIVATE malformed summary bytes",
        json.dumps(
            {
                "overview": {"text": "PRIVATE claim", "evidence_span_ids": []},
                "key_topics": [],
                "decisions": [],
                "action_items": [],
                "provider": "fake",
                "model": "fake-summary",
            }
        ).encode(),
        _summary_content("Valid shape with unknown evidence").replace(b"segment-1", b"unknown"),
    ],
)
def test_invalid_summary_completion_preserves_current_summary(
    monkeypatch: pytest.MonkeyPatch,
    invalid_content: bytes,
) -> None:
    run(_test_invalid_summary_completion_preserves_current_summary, monkeypatch, invalid_content)


async def _test_invalid_summary_completion_preserves_current_summary(
    monkeypatch: pytest.MonkeyPatch,
    invalid_content: bytes,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="prior-summary.json",
                sha256="d" * 64,
                byte_size=10,
                source_version=1,
                transcript_version=1,
                is_current=True,
            )
        )
    manager = _manager(sessions, CompletionObjectStore(invalid_content), UnusedCanonicalStore())

    with pytest.raises(CompletionRejectedError, match="summary artifact or evidence is invalid") as error:
        await manager.apply_completion(completion)

    assert "PRIVATE" not in str(error.value)
    with sessions() as db:
        assert db.get(Job, job_id).state is ProcessingState.QUEUED
        summaries = db.query(ArtifactManifest).filter_by(kind=ArtifactKind.SUMMARY).all()
        assert len(summaries) == 1
        assert summaries[0].object_key == "prior-summary.json"
        assert summaries[0].is_current is True
    engine.dispose()


@pytest.mark.unit
def test_old_mapping_summary_success_cannot_be_promoted_after_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject a running summary whose immutable speaker mapping revision has become stale."""
    run(_test_old_mapping_summary_success_cannot_be_promoted_after_rename, monkeypatch)


async def _test_old_mapping_summary_success_cannot_be_promoted_after_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        db.get(Conversation, conversation_id).speaker_revision = 1
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.SUMMARY,
                version=1,
                object_key="current-mapping-summary.json",
                sha256="d" * 64,
                byte_size=10,
                source_version=1,
                transcript_version=1,
                speaker_revision=1,
                is_current=True,
            )
        )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    with pytest.raises(CompletionRejectedError, match="speaker mapping is no longer current"):
        await manager.apply_completion(completion)

    with sessions() as db:
        assert db.get(Job, job_id).state is ProcessingState.QUEUED
        assert db.get(Conversation, conversation_id).sync_state is SyncState.PROCESSING
        current_summary = db.query(ArtifactManifest).filter_by(
            kind=ArtifactKind.SUMMARY,
            is_current=True,
        ).one()
        assert current_summary.object_key == "current-mapping-summary.json"
    engine.dispose()


@pytest.mark.unit
def test_old_mapping_summary_failure_does_not_fail_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Record an old summary failure without regressing the renamed conversation state."""
    run(_test_old_mapping_summary_failure_does_not_fail_new_revision, monkeypatch)


async def _test_old_mapping_summary_failure_does_not_fail_new_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, _, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        db.get(Conversation, conversation_id).speaker_revision = 1
    failure = CompletionEvent(
        job_id=job_id,
        conversation_id=conversation_id,
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        speaker_revision=0,
        state=ProcessingState.FAILED,
        error_code=ProcessingErrorCode.UNKNOWN,
        error_details="summary failed",
    )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    await manager.apply_completion(failure)

    with sessions() as db:
        assert db.get(Job, job_id).state is ProcessingState.FAILED
        assert db.get(Conversation, conversation_id).sync_state is SyncState.PROCESSING
    engine.dispose()


@pytest.mark.unit
def test_late_completion_for_cancelled_summary_is_recorded_once_and_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(_test_late_completion_for_cancelled_summary_is_recorded_once_and_discarded, monkeypatch)


async def _test_late_completion_for_cancelled_summary_is_recorded_once_and_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        db.get(Job, job_id).state = ProcessingState.CANCELLED
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    await manager.apply_completion(completion)
    await manager.apply_completion(completion)

    with sessions() as db:
        job = db.get(Job, job_id)
        attempts = db.query(ProcessingAttempt).filter_by(job_id=job_id).all()
        assert job is not None
        assert job.state is ProcessingState.CANCELLED
        assert job.attempts == 1
        assert len(attempts) == 1
        assert attempts[0].state is ProcessingState.CANCELLED
        assert db.query(ArtifactManifest).filter_by(kind=ArtifactKind.SUMMARY).count() == 0
    engine.dispose()


@pytest.mark.unit
def test_duplicate_summary_completion_is_rejected_after_first_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply one success once and reject its duplicate without creating another artifact version."""
    run(_test_duplicate_summary_completion_is_rejected_after_first_promotion, monkeypatch)


async def _test_duplicate_summary_completion_is_rejected_after_first_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, content = _seed_summary_completion(monkeypatch)
    artifact = StoredConversationArtifact(ArtifactKind.SUMMARY, "summary-v1.json", "c" * 64)
    manager = _manager(sessions, CompletionObjectStore(content), CompletionCanonicalStore(artifact))

    await manager.apply_completion(completion)
    with pytest.raises(CompletionRejectedError, match="job has already completed"):
        await manager.apply_completion(completion)

    with sessions() as db:
        summaries = db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all()
        assert len(summaries) == 1
        assert summaries[0].is_current is True
        assert summaries[0].transcript_version == 1
        assert db.get(Job, job_id).state is ProcessingState.SUCCEEDED
    engine.dispose()


@pytest.mark.unit
def test_transcript_promotion_invalidates_prior_summary_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retire the old summary in the same commit that promotes a new transcript version."""
    run(_test_transcript_promotion_invalidates_prior_summary_atomically, monkeypatch)


async def _test_transcript_promotion_invalidates_prior_summary_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    job_id = uuid4()
    with sessions.begin() as db:
        db.add(
            Conversation(
                id=conversation_id,
                owner_subject="owner",
                occurred_at=datetime.now(timezone.utc),
                sync_state=SyncState.PROCESSING,
                visibility=VisibilityState.ACTIVE,
                speaker_revision=7,
            )
        )
        db.add_all(
            [
                Job(
                    id=job_id,
                    conversation_id=conversation_id,
                    kind=ProcessingKind.TRANSCRIPTION,
                    source_version=1,
                    state=ProcessingState.QUEUED,
                ),
                ArtifactManifest(
                    conversation_id=conversation_id,
                    kind=ArtifactKind.TRANSCRIPT,
                    version=1,
                    object_key="transcript-v1.json",
                    sha256="a" * 64,
                    byte_size=10,
                    source_version=1,
                    is_current=True,
                ),
                ArtifactManifest(
                    conversation_id=conversation_id,
                    kind=ArtifactKind.SUMMARY,
                    version=1,
                    object_key="summary-v1.json",
                    sha256="b" * 64,
                    byte_size=10,
                    source_version=1,
                    transcript_version=1,
                    is_current=True,
                ),
            ]
        )
        db.add(
            ConversationSpeaker(
                conversation_id=conversation_id,
                speaker_number=1,
                display_name="Old transcript name",
            )
        )

    content = b'{"segments":[{"evidence_id":"segment-1","speaker":"Speaker 1","start_seconds":0,"end_seconds":1,"text":"Hello."}],"provider":"fake","model":"fake-transcription"}'
    completion = CompletionEvent(
        job_id=job_id,
        conversation_id=conversation_id,
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        state=ProcessingState.SUCCEEDED,
        result_object=ResultObject(
            kind=ArtifactKind.TRANSCRIPT,
            bucket="results",
            key="transcript-result.json",
            sha256="c" * 64,
            byte_size=len(content),
        ),
    )
    artifact = StoredConversationArtifact(ArtifactKind.TRANSCRIPT, "transcript-v2.json", "c" * 64)
    manager = _manager(sessions, CompletionObjectStore(content), CompletionCanonicalStore(artifact))

    await manager.apply_completion(completion)

    with sessions() as db:
        summaries = db.scalars(
            select(ArtifactManifest).where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
        ).all()
        current_transcript = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
                ArtifactManifest.is_current.is_(True),
            )
        )
        next_summary_job = db.scalar(
            select(Job).where(
                Job.conversation_id == conversation_id,
                Job.kind == ProcessingKind.SUMMARIZATION,
            )
        )
        assert len(summaries) == 1
        assert summaries[0].is_current is False
        assert current_transcript is not None
        assert current_transcript.version == 2
        assert next_summary_job is not None
        assert next_summary_job.transcript_version == 2
        assert next_summary_job.speaker_revision == 8
        assert next_summary_job.speaker_mapping == {}
        promoted_conversation = db.get(Conversation, conversation_id)
        assert promoted_conversation is not None
        assert promoted_conversation.speaker_revision == 8
        speakers = db.query(ConversationSpeaker).filter_by(conversation_id=conversation_id).all()
        assert [(speaker.speaker_number, speaker.display_name) for speaker in speakers] == [(1, None)]
        summary_outbox = db.query(OutboxMessage).filter_by(conversation_id=conversation_id).one()
        assert summary_outbox.payload["speaker_revision"] == 8
        assert summary_outbox.payload["speaker_mapping"] == {}

    summary_content = _summary_content("updated")
    summary_completion = CompletionEvent(
        job_id=next_summary_job.id,
        conversation_id=conversation_id,
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=2,
        speaker_revision=8,
        state=ProcessingState.SUCCEEDED,
        result_object=ResultObject(
            kind=ArtifactKind.SUMMARY,
            bucket="results",
            key="summary-result.json",
            sha256="e" * 64,
            byte_size=len(summary_content),
        ),
    )
    summary_artifact = StoredConversationArtifact(ArtifactKind.SUMMARY, "summary-v2.json", "e" * 64)
    summary_manager = _manager(
        sessions,
        CompletionObjectStore(summary_content),
        CompletionCanonicalStore(summary_artifact),
    )
    await summary_manager.apply_completion(summary_completion)

    with sessions() as db:
        summaries = db.scalars(
            select(ArtifactManifest)
            .where(ArtifactManifest.kind == ArtifactKind.SUMMARY)
            .order_by(ArtifactManifest.version)
        ).all()
        assert [(summary.version, summary.is_current, summary.speaker_revision) for summary in summaries] == [
            (1, False, 0),
            (2, True, 8),
        ]
    engine.dispose()


@pytest.mark.unit
def test_stale_failed_summary_after_transcript_change_preserves_state_and_attempt_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(_test_stale_failed_summary_after_transcript_change_preserves_state_and_attempt_audit, monkeypatch)


async def _test_stale_failed_summary_after_transcript_change_preserves_state_and_attempt_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        conversation = db.get(Conversation, conversation_id)
        conversation.sync_state = SyncState.PROCESSING
        transcript = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
                ArtifactManifest.is_current.is_(True),
            )
        )
        transcript.is_current = False
        db.add(
            ArtifactManifest(
                conversation_id=conversation_id,
                kind=ArtifactKind.TRANSCRIPT,
                version=2,
                object_key="transcript-v2.json",
                sha256="d" * 64,
                byte_size=10,
                source_version=1,
                is_current=True,
            )
        )
        db.add(
            Job(
                conversation_id=conversation_id,
                kind=ProcessingKind.SUMMARIZATION,
                source_version=1,
                transcript_version=2,
                state=ProcessingState.QUEUED,
            )
        )
    failed_completion = completion.model_copy(
        update={
            "state": ProcessingState.FAILED,
            "result_object": None,
            "error_code": ProcessingErrorCode.PROVIDER_UNAVAILABLE,
            "error_details": "provider unavailable",
        }
    )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    await manager.apply_completion(failed_completion)
    with pytest.raises(CompletionRejectedError, match="job has already completed"):
        await manager.apply_completion(failed_completion)

    with sessions() as db:
        failed_job = db.get(Job, job_id)
        conversation = db.get(Conversation, conversation_id)
        attempts = db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.job_id == job_id)).all()
        assert failed_job.state is ProcessingState.FAILED
        assert failed_job.attempts == 1
        assert len(attempts) == 1
        assert attempts[0].state is ProcessingState.FAILED
        assert attempts[0].error_details == "provider unavailable"
        assert conversation.sync_state is SyncState.PROCESSING
    engine.dispose()


@pytest.mark.unit
def test_stale_failed_summary_after_newer_rerun_preserves_current_summary_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run(_test_stale_failed_summary_after_newer_rerun_preserves_current_summary_and_state, monkeypatch)


async def _test_stale_failed_summary_after_newer_rerun_preserves_current_summary_and_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, conversation_id, job_id, completion, _ = _seed_summary_completion(monkeypatch)
    with sessions.begin() as db:
        conversation = db.get(Conversation, conversation_id)
        conversation.sync_state = SyncState.READY
        db.add_all(
            [
                Job(
                    conversation_id=conversation_id,
                    kind=ProcessingKind.SUMMARIZATION,
                    source_version=1,
                    transcript_version=1,
                    state=ProcessingState.SUCCEEDED,
                    attempts=1,
                ),
                ArtifactManifest(
                    conversation_id=conversation_id,
                    kind=ArtifactKind.SUMMARY,
                    version=2,
                    object_key="summary-v2.json",
                    sha256="e" * 64,
                    byte_size=10,
                    source_version=1,
                    transcript_version=1,
                    is_current=True,
                ),
            ]
        )
    failed_completion = completion.model_copy(
        update={
            "state": ProcessingState.FAILED,
            "result_object": None,
            "error_code": ProcessingErrorCode.PROVIDER_UNAVAILABLE,
            "error_details": "provider unavailable",
            "emitted_at": completion.emitted_at - timedelta(seconds=1),
        }
    )
    manager = _manager(sessions, UnusedObjectStore(), UnusedCanonicalStore())

    await manager.apply_completion(failed_completion)
    with pytest.raises(CompletionRejectedError, match="job has already completed"):
        await manager.apply_completion(failed_completion)

    with sessions() as db:
        failed_job = db.get(Job, job_id)
        conversation = db.get(Conversation, conversation_id)
        current_summary = db.scalar(
            select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == conversation_id,
                ArtifactManifest.kind == ArtifactKind.SUMMARY,
                ArtifactManifest.is_current.is_(True),
            )
        )
        attempts = db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.job_id == job_id)).all()
        assert failed_job.state is ProcessingState.FAILED
        assert failed_job.attempts == 1
        assert len(attempts) == 1
        assert attempts[0].state is ProcessingState.FAILED
        assert attempts[0].error_details == "provider unavailable"
        assert conversation.sync_state is SyncState.READY
        assert current_summary is not None
        assert current_summary.version == 2
        assert current_summary.is_current is True
    engine.dispose()

