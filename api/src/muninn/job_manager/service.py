"""Implement job dispatch and completion application services."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from buzz import enforce_defined
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from muninn.constants import ArtifactKind, ProcessingKind, ProcessingState, SyncState
from muninn.db.models import ArtifactManifest, Conversation, ConversationSpeaker, Job, OutboxMessage, ProcessingAttempt, Tombstone
from muninn.job_manager.exceptions import CompletionRejectedError
from muninn.job_manager.lifecycle import cancel_job, record_cancelled_attempt, supersede_summarization
from muninn.job_manager.schemas import CompletionRejectionContext, ManifestVersion, SessionFactory, ValidatedCompletion
from muninn.logging import log_error
from muninn.queue.constants import NatsSubject, SUMMARY_OUTBOX_DEDUPLICATION_KEY
from muninn.queue.contracts import CompletionEvent, JobMessage, OutboxJobPayload
from muninn.queue.exceptions import (
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFoundError,
    ObjectStoreOperationError,
    ObjectStoreUnavailableError,
)
from muninn.queue.manager import (
    CONSUMER_IDLE_INTERVAL_SECONDS,
    OUTBOX_DISPATCH_INTERVAL_SECONDS,
    SUBSCRIPTION_FETCH_TIMEOUT_SECONDS,
    QueueManager,
)
from muninn.schemas import SummaryResult, TranscriptResult, validate_summary_evidence
from muninn.storage import ObjectStore, StoredConversationArtifact


@dataclass(slots=True)
class JobManager:
    """Coordinate queue, object-store, and database work for processing jobs."""

    queue: QueueManager
    canonical_store: ObjectStore
    session_factory: SessionFactory

    def _outbox_dispatch_state(
        self, db: Session, message_id: UUID, payload: OutboxJobPayload, lock: bool
    ) -> tuple[bool, Job | None, OutboxMessage | None]:
        if lock and db.get_bind().dialect.name == "sqlite":
            db.execute(
                update(Conversation)
                .where(Conversation.id == payload.conversation_id)
                .values(speaker_revision=Conversation.speaker_revision)
            )
        conversation_query = select(Conversation).where(Conversation.id == payload.conversation_id)
        job_query = select(Job).where(Job.id == payload.job_id)
        outbox_query = select(OutboxMessage).where(
            OutboxMessage.id == message_id,
            OutboxMessage.published_at.is_(None),
        )
        if lock:
            conversation_query = conversation_query.with_for_update()
            job_query = job_query.with_for_update()
            outbox_query = outbox_query.with_for_update()
        conversation = db.scalar(conversation_query)
        job = db.scalar(job_query)
        outbox = db.scalar(outbox_query)
        tombstone = db.scalar(select(Tombstone).where(Tombstone.conversation_id == payload.conversation_id))
        current = (
            outbox is not None
            and outbox.conversation_id == payload.conversation_id
            and conversation is not None
            and tombstone is None
            and job is not None
            and job.state in {ProcessingState.QUEUED, ProcessingState.RUNNING}
            and job.conversation_id == payload.conversation_id
            and job.kind is payload.kind
            and job.source_version == payload.source_version
            and job.transcript_version == payload.transcript_version
            and job.speaker_revision == payload.speaker_revision
        )
        if current and payload.kind is ProcessingKind.SUMMARIZATION:
            transcript_query = select(ArtifactManifest).where(
                ArtifactManifest.conversation_id == payload.conversation_id,
                ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
                ArtifactManifest.is_current.is_(True),
            )
            if lock:
                transcript_query = transcript_query.with_for_update()
            transcript = db.scalar(transcript_query)
            current = (
                transcript is not None
                and transcript.source_version == payload.source_version
                and transcript.version == payload.transcript_version
                and transcript.object_key == payload.source_key
                and conversation.speaker_revision == payload.speaker_revision
            )
        return current, job, outbox

    async def _delete_unpublished_input(self, key: str, outbox_id: UUID, conversation_id: UUID) -> None:
        try:
            await self.queue.object_store.delete_key(key)
        except ObjectStoreError as exception:
            log_error(
                exception,
                "Unpublished queue input cleanup failed",
                context=dict(outbox_id=outbox_id, conversation_id=conversation_id),
            )

    async def dispatch_outbox_once(self) -> int:
        """Copy current source objects to the queue object store and publish jobs."""
        with self.session_factory(False) as db:
            messages = list(db.scalars(
                select(OutboxMessage)
                .join(Tombstone, Tombstone.conversation_id == OutboxMessage.conversation_id, isouter=True)
                .where(OutboxMessage.published_at.is_(None), Tombstone.conversation_id.is_(None))
                .limit(25)
            ))

        count = 0
        for message in messages:
            try:
                payload = OutboxJobPayload.model_validate(message.payload)
            except (TypeError, ValueError) as exception:
                log_error(exception, "Outbox dispatch failed while preparing a job; dispatch will retry/skip this row",
                          context=dict(outbox_id=message.id, conversation_id=message.conversation_id))
                continue

            with self.session_factory(False) as db:
                current, _, _ = self._outbox_dispatch_state(db, message.id, payload, lock=False)
            if not current:
                with self.session_factory(in_transaction=True) as db:
                    current, job, outbox = self._outbox_dispatch_state(db, message.id, payload, lock=True)
                    if outbox is not None and not current:
                        if job is not None and job.conversation_id == payload.conversation_id:
                            cancel_job(db, job)
                        outbox.published_at = datetime.now(timezone.utc)
                continue

            reference = None
            input_key = None
            try:
                source = self.canonical_store.get_bytes(payload.source_key)
                input_key = f"inputs/{message.id}/{uuid4()}"
                reference = await self.queue.object_store.put(
                    input_key,
                    source,
                    ArtifactKind.RECORDING if payload.kind is ProcessingKind.TRANSCRIPTION else ArtifactKind.TRANSCRIPT,
                )
                queued_job = JobMessage(
                    job_id=payload.job_id,
                    conversation_id=payload.conversation_id,
                    kind=payload.kind,
                    source_version=payload.source_version,
                    transcript_version=payload.transcript_version,
                    speaker_revision=payload.speaker_revision,
                    speaker_mapping=payload.speaker_mapping,
                    source_object=reference,
                )
            except (TypeError, ValueError, OSError, ObjectStoreError) as exception:
                if input_key is not None:
                    await self._delete_unpublished_input(input_key, message.id, message.conversation_id)
                log_error(exception, "Outbox dispatch failed while preparing a job; dispatch will retry/skip this row",
                          context=dict(outbox_id=message.id, conversation_id=message.conversation_id))
                continue
            except Exception:
                if input_key is not None:
                    await self._delete_unpublished_input(input_key, message.id, message.conversation_id)
                raise

            discard_reference = False
            publish_started = False
            try:
                with self.session_factory(in_transaction=True) as db:
                    current, job, outbox = self._outbox_dispatch_state(db, message.id, payload, lock=True)
                    if outbox is None:
                        discard_reference = True
                    elif not current:
                        if job is not None and job.conversation_id == payload.conversation_id:
                            cancel_job(db, job)
                        outbox.published_at = datetime.now(timezone.utc)
                        discard_reference = True
                    else:
                        publish_started = True
                        await self.queue.publish_job(queued_job)
                        outbox.published_at = datetime.now(timezone.utc)
                        outbox.attempts += 1
                        count += 1
            except Exception:
                if not publish_started:
                    await self._delete_unpublished_input(reference.key, message.id, message.conversation_id)
                raise
            if discard_reference:
                await self._delete_unpublished_input(reference.key, message.id, message.conversation_id)
        return count

    async def dispatch_outbox(self) -> None:
        """Continuously dispatch pending outbox rows."""
        while True:
            await self.dispatch_outbox_once()
            await asyncio.sleep(OUTBOX_DISPATCH_INTERVAL_SECONDS)

    def _completion_context(self, event: CompletionEvent) -> CompletionRejectionContext:
        """Build safe event identifiers for completion rejection messages."""
        return CompletionRejectionContext(
            job_id=event.job_id, conversation_id=event.conversation_id, event_kind=event.kind.value,
            event_source_version=event.source_version, event_transcript_version=event.transcript_version,
        )

    def _lock_and_validate_completion(
        self, db: Session, event: CompletionEvent, lock: bool = True
    ) -> ValidatedCompletion:
        """Validate the conversation and job, locking them when requested."""
        context = self._completion_context(event)
        if lock and db.get_bind().dialect.name == "sqlite":
            db.execute(
                update(Conversation)
                .where(Conversation.id == event.conversation_id)
                .values(
                    speaker_revision=Conversation.speaker_revision,
                    updated_at=Conversation.updated_at,
                )
            )
        conversation_query = select(Conversation).where(Conversation.id == event.conversation_id)
        if lock:
            conversation_query = conversation_query.with_for_update()
        conversation = CompletionRejectedError.enforce_defined(
            db.scalar(conversation_query),
            "completion rejected: conversation row is missing", raise_kwargs={"context": context})
        CompletionRejectedError.require_condition(
            db.scalar(select(Tombstone).where(Tombstone.conversation_id == event.conversation_id)) is None,
            "completion rejected: conversation is tombstoned", raise_kwargs={"context": context})
        job_query = select(Job).where(Job.id == event.job_id)
        if lock:
            job_query = job_query.with_for_update()
        job = CompletionRejectedError.enforce_defined(
            db.scalar(job_query),
            "completion rejected: job row is missing", raise_kwargs={"context": context})
        context = context.with_job(job.kind.value, job.source_version, job.transcript_version)
        with CompletionRejectedError.check_expressions(
            "completion rejected: completion does not match the current job", raise_kwargs={"context": context}
        ) as check:
            check(job.conversation_id == event.conversation_id, "job conversation ID must match event conversation ID")
            check(job.kind is event.kind, "job kind must match event kind")
            check(job.source_version == event.source_version, "job source version must match event source version")
            check(job.transcript_version == event.transcript_version, "job transcript version must match event transcript version")
            check(job.speaker_revision == event.speaker_revision, "job speaker revision must match event speaker revision")
        CompletionRejectedError.require_condition(
            event.state is ProcessingState.RUNNING or job.state not in {ProcessingState.SUCCEEDED, ProcessingState.FAILED},
            "completion rejected: job has already completed", raise_kwargs={"context": context})
        return ValidatedCompletion(conversation, job)

    def _record_processing_attempt(self, db: Session, job: Job, event: CompletionEvent) -> None:
        """Persist one worker attempt and its safe terminal error details."""
        attempt_number = job.attempts + 1
        attempt = db.scalar(
            select(ProcessingAttempt)
            .where(ProcessingAttempt.job_id == job.id, ProcessingAttempt.attempt_number == attempt_number)
            .with_for_update()
        )
        if attempt is None:
            attempt = ProcessingAttempt(
                conversation_id=job.conversation_id,
                job_id=job.id,
                kind=job.kind,
                attempt_number=attempt_number,
                state=event.state,
                started_at=event.emitted_at if event.state is ProcessingState.RUNNING else job.created_at,
            )
            db.add(attempt)
        if event.state is not ProcessingState.RUNNING:
            attempt.state = event.state
            attempt.error_code = event.error_code
            attempt.error_details = event.error_details
            attempt.finished_at = event.emitted_at

    def _should_mark_conversation_failed(self, db: Session, event: CompletionEvent) -> bool:
        """Check whether a failed summary still governs the current conversation state."""
        if event.kind is not ProcessingKind.SUMMARIZATION:
            return True
        transcript = db.scalar(
            select(ArtifactManifest)
            .where(
                ArtifactManifest.conversation_id == event.conversation_id,
                ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
                ArtifactManifest.is_current.is_(True),
            )
            .with_for_update()
        )
        current_speaker_revision = db.scalar(
            select(Conversation.speaker_revision).where(Conversation.id == event.conversation_id)
        )
        if (
            transcript is None
            or transcript.source_version != event.source_version
            or transcript.version != event.transcript_version
            or current_speaker_revision != event.speaker_revision
        ):
            return False
        summary = db.scalar(
            select(ArtifactManifest)
            .where(
                ArtifactManifest.conversation_id == event.conversation_id,
                ArtifactManifest.kind == ArtifactKind.SUMMARY,
                ArtifactManifest.is_current.is_(True),
            )
            .with_for_update()
        )
        if (
            summary is None
            or summary.source_version != event.source_version
            or summary.transcript_version != event.transcript_version
            or summary.speaker_revision != event.speaker_revision
        ):
            return True
        summary_created_at = summary.created_at
        if summary_created_at.tzinfo is None:
            summary_created_at = summary_created_at.replace(tzinfo=timezone.utc)
        event_emitted_at = event.emitted_at
        if event_emitted_at.tzinfo is None:
            event_emitted_at = event_emitted_at.replace(tzinfo=timezone.utc)
        return summary_created_at <= event_emitted_at

    def _apply_failed_completion(self, db: Session, conversation: Conversation, job: Job, event: CompletionEvent) -> None:
        """Persist failed job and attempt diagnostics without regressing newer conversation state."""
        self._record_processing_attempt(db, job, event)
        db.execute(update(Job).where(Job.id == job.id).values(state=ProcessingState.FAILED, attempts=Job.attempts + 1))
        if self._should_mark_conversation_failed(db, event):
            db.execute(
                update(Conversation).where(Conversation.id == conversation.id).values(sync_state=SyncState.FAILED)
            )

    def _current_transcript_manifest(
        self, db: Session, event: CompletionEvent, lock: bool = True
    ) -> ArtifactManifest:
        """Validate and return the current transcript manifest for a summary event."""
        transcript_query = select(ArtifactManifest).where(
            ArtifactManifest.conversation_id == event.conversation_id,
            ArtifactManifest.kind == ArtifactKind.TRANSCRIPT,
            ArtifactManifest.is_current.is_(True),
        )
        if lock:
            transcript_query = transcript_query.with_for_update()
        transcript = CompletionRejectedError.enforce_defined(
            db.scalar(transcript_query),
            "completion rejected: summary transcript is no longer current",
            raise_kwargs={"context": self._completion_context(event)},
        )
        CompletionRejectedError.require_condition(
            transcript.source_version == event.source_version
            and transcript.version == event.transcript_version,
            "completion rejected: summary transcript is no longer current",
            raise_kwargs={"context": self._completion_context(event)},
        )
        current_speaker_revision = db.scalar(
            select(Conversation.speaker_revision).where(Conversation.id == event.conversation_id)
        )
        CompletionRejectedError.require_condition(
            current_speaker_revision == event.speaker_revision,
            "completion rejected: speaker mapping is no longer current",
            raise_kwargs={"context": self._completion_context(event)},
        )
        return transcript

    def _lock_current_manifest(
        self, db: Session, event: CompletionEvent, lock: bool = True
    ) -> ManifestVersion:
        """Validate current source lineage and optionally lock the destination manifests."""
        kind = ArtifactKind.TRANSCRIPT if event.kind is ProcessingKind.TRANSCRIPTION else ArtifactKind.SUMMARY
        if event.kind is ProcessingKind.SUMMARIZATION:
            self._current_transcript_manifest(db, event, lock)
        current_query = select(ArtifactManifest).where(
            ArtifactManifest.conversation_id == event.conversation_id,
            ArtifactManifest.kind == kind,
            ArtifactManifest.is_current.is_(True),
        )
        if lock:
            current_query = current_query.with_for_update()
        current = db.scalar(current_query)
        latest_version = db.scalar(
            select(func.max(ArtifactManifest.version)).where(
                ArtifactManifest.conversation_id == event.conversation_id,
                ArtifactManifest.kind == kind,
            )
        )
        return ManifestVersion(
            kind=kind,
            current=current,
            next_version=latest_version + 1 if latest_version is not None else 1,
        )

    async def _retrieve_result(self, event: CompletionEvent) -> bytes:
        """Retrieve the validated result object from the queue object store."""
        result_object = enforce_defined(
            event.result_object, "completion rejected: successful completion has no result object",
            raise_exc_class=CompletionRejectedError, raise_kwargs={"context": self._completion_context(event)})
        return await self.queue.object_store.get(result_object)

    def _store_canonical_artifact(self, event: CompletionEvent, content: bytes) -> StoredConversationArtifact:
        """Store result bytes canonically and log failures at the storage boundary."""
        try:
            return self.canonical_store.put_conversation_artifact(event, content)
        except Exception as exc:
            log_error(exc, "completion rejected: canonical artifact storage failed "
                      f"job_id={event.job_id} conversation_id={event.conversation_id} completion_kind={event.kind.value}")
            raise

    def _persist_success(
        self,
        db: Session,
        event: CompletionEvent,
        conversation: Conversation,
        job: Job,
        stored_artifact: StoredConversationArtifact,
        content: bytes,
        manifest_version: ManifestVersion,
        transcript: TranscriptResult | None,
    ) -> None:
        """Persist the promoted artifact and all dependent state in the open transaction."""
        try:
            summary_speaker_revision = event.speaker_revision
            if event.kind is ProcessingKind.TRANSCRIPTION:
                supersede_summarization(db, event.conversation_id)
                db.execute(
                    update(ArtifactManifest)
                    .where(
                        ArtifactManifest.conversation_id == event.conversation_id,
                        ArtifactManifest.kind == ArtifactKind.SUMMARY,
                        ArtifactManifest.is_current.is_(True),
                    )
                    .values(is_current=False)
                )
                if transcript is None:
                    raise CompletionRejectedError(
                        "completion rejected: transcript artifact is invalid",
                        context=self._completion_context(event),
                    )
                db.query(ConversationSpeaker).filter_by(conversation_id=event.conversation_id).delete(
                    synchronize_session=False
                )
                speaker_numbers = sorted(
                    {int(segment.speaker.removeprefix("Speaker ")) for segment in transcript.segments}
                )
                db.add_all(
                    ConversationSpeaker(conversation_id=event.conversation_id, speaker_number=speaker_number)
                    for speaker_number in speaker_numbers
                )
                conversation.speaker_revision += 1
                summary_speaker_revision = conversation.speaker_revision
            if manifest_version.current is not None:
                db.execute(
                    update(ArtifactManifest)
                    .where(ArtifactManifest.id == manifest_version.current.id)
                    .values(is_current=False)
                )
            db.add(
                ArtifactManifest(
                    conversation_id=event.conversation_id,
                    kind=manifest_version.kind,
                    version=manifest_version.next_version,
                    object_key=stored_artifact.key,
                    sha256=stored_artifact.sha256,
                    byte_size=len(content),
                    source_version=event.source_version,
                    transcript_version=event.transcript_version,
                    speaker_revision=event.speaker_revision,
                    is_current=True,
                )
            )
            if event.kind is ProcessingKind.TRANSCRIPTION:
                sync_state = SyncState.PROCESSING
                summary_job = Job(
                    conversation_id=event.conversation_id,
                    kind=ProcessingKind.SUMMARIZATION,
                    source_version=event.source_version,
                    transcript_version=manifest_version.next_version,
                    speaker_revision=summary_speaker_revision,
                    speaker_mapping={},
                )
                db.add(summary_job)
                db.flush()
                db.add(
                    OutboxMessage(
                        conversation_id=event.conversation_id,
                        subject=NatsSubject.JOBS,
                        payload=OutboxJobPayload(
                            job_id=summary_job.id,
                            conversation_id=event.conversation_id,
                            kind=ProcessingKind.SUMMARIZATION,
                            source_version=event.source_version,
                            transcript_version=manifest_version.next_version,
                            speaker_revision=summary_speaker_revision,
                            speaker_mapping={},
                            source_key=stored_artifact.key,
                        ).model_dump(mode="json", exclude_none=True),
                        deduplication_key=SUMMARY_OUTBOX_DEDUPLICATION_KEY.format(
                            conversation_id=event.conversation_id,
                            source_version=event.source_version,
                            transcript_version=manifest_version.next_version,
                            speaker_revision=summary_speaker_revision,
                            job_id=summary_job.id,
                        ),
                    )
                )
            else:
                sync_state = SyncState.READY
            conversation.sync_state = sync_state
            self._record_processing_attempt(db, job, event)
            db.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(state=ProcessingState.SUCCEEDED, attempts=Job.attempts + 1)
            )
            db.flush()
        except Exception as exc:
            log_error(
                exc,
                "Final database persistence failed for "
                f"job_id={event.job_id} conversation_id={event.conversation_id} kind={manifest_version.kind.value} version={manifest_version.next_version}",
            )
            raise

    async def apply_completion(self, event: CompletionEvent) -> None:
        """Verify, promote, and persist one worker completion atomically."""
        content = None
        transcript = None
        source_transcript = None
        if event.state is ProcessingState.SUCCEEDED:
            with self.session_factory(False) as db:
                preflight = self._lock_and_validate_completion(db, event, lock=False)
                transcript_key = None
                if preflight.job.state is not ProcessingState.CANCELLED:
                    if event.kind is ProcessingKind.SUMMARIZATION:
                        transcript_key = self._current_transcript_manifest(db, event, lock=False).object_key
            if preflight.job.state is not ProcessingState.CANCELLED:
                if transcript_key is not None:
                    transcript_bytes = self.canonical_store.get_bytes(transcript_key)
                    try:
                        source_transcript = TranscriptResult.model_validate_json(transcript_bytes)
                    except (ValidationError, ValueError):
                        raise CompletionRejectedError(
                            "completion rejected: current transcript artifact is invalid",
                            context=self._completion_context(event),
                        ) from None
                content = await self._retrieve_result(event)
                if event.kind is ProcessingKind.TRANSCRIPTION:
                    try:
                        transcript = TranscriptResult.model_validate_json(content)
                    except (ValidationError, ValueError) as exc:
                        raise CompletionRejectedError(
                            "completion rejected: transcript artifact is invalid",
                            context=self._completion_context(event),
                        ) from exc
                elif event.kind is ProcessingKind.SUMMARIZATION:
                    try:
                        summary = SummaryResult.model_validate_json(content)
                        validate_summary_evidence(summary, enforce_defined(source_transcript, "current transcript is missing"))
                    except (ValidationError, ValueError):
                        raise CompletionRejectedError(
                            "completion rejected: summary artifact or evidence is invalid",
                            context=self._completion_context(event),
                        ) from None

        with self.session_factory(in_transaction=True) as db:
            validated = self._lock_and_validate_completion(db, event)
            if validated.job.state is ProcessingState.CANCELLED:
                record_cancelled_attempt(db, validated.job, event.emitted_at)
                return
            if event.state is ProcessingState.RUNNING:
                if validated.job.state in {ProcessingState.QUEUED, ProcessingState.RUNNING}:
                    self._record_processing_attempt(db, validated.job, event)
                    db.execute(
                        update(Job)
                        .where(Job.id == validated.job.id, Job.state == ProcessingState.QUEUED)
                        .values(state=ProcessingState.RUNNING)
                    )
                return
            if event.state is ProcessingState.FAILED:
                self._apply_failed_completion(db, validated.conversation, validated.job, event)
                return
            manifest_version = self._lock_current_manifest(db, event)
            content = enforce_defined(
                content,
                "completion rejected: successful completion has no result object",
                raise_exc_class=CompletionRejectedError,
                raise_kwargs={"context": self._completion_context(event)},
            )
            stored_artifact = self._store_canonical_artifact(event, content)
            self._persist_success(
                db,
                event,
                validated.conversation,
                validated.job,
                stored_artifact,
                content,
                manifest_version,
                transcript,
            )

    async def consume_completions(self) -> None:
        """Consume completion events and apply explicit message dispositions."""
        subscription = await self.queue.subscribe_completions()
        while True:
            try:
                messages = await subscription.fetch(10, timeout=SUBSCRIPTION_FETCH_TIMEOUT_SECONDS)
            except TimeoutError:
                await asyncio.sleep(CONSUMER_IDLE_INTERVAL_SECONDS)
                continue
            for message in messages:
                try:
                    event = CompletionEvent.model_validate_json(message.data)
                except ValueError as exc:
                    log_error(exc, "Malformed completion message; dead-lettering it")
                    await self.queue.publish_dead_letter(message.data, "malformed_completion")
                    await message.term()
                    continue
                try:
                    await self.apply_completion(event)
                except CompletionRejectedError as exc:
                    log_error(exc, "Completion rejected; dead-lettering it")
                    await self.queue.publish_dead_letter(message.data, "completion_rejected")
                    await message.term()
                    continue
                except ObjectStoreUnavailableError as exc:
                    log_error(exc, "Transient Object Store failure while applying completion; NAKing for retry")
                    await message.nak()
                    continue
                except (ObjectStoreNotFoundError, ObjectStoreIntegrityError, ObjectStoreOperationError) as exc:
                    log_error(exc, "Permanent Object Store failure while applying completion; dead-lettering it")
                    await self.queue.publish_dead_letter(message.data, "completion_object_store_failure")
                    await message.term()
                    continue
                await message.ack()
            await asyncio.sleep(CONSUMER_IDLE_INTERVAL_SECONDS)
