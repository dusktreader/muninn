"""Manage superseded summary jobs and their pending publications."""

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from muninn.constants import ProcessingKind, ProcessingState
from muninn.db.models import Job, OutboxMessage, ProcessingAttempt


def cancel_job(db: Session, job: Job, now: datetime | None = None) -> None:
    """Cancel one active job and close its persisted running attempt, if any."""
    if job.state not in {ProcessingState.QUEUED, ProcessingState.RUNNING}:
        return
    finished_at = now or datetime.now(timezone.utc)
    attempt = db.scalar(
        select(ProcessingAttempt)
        .where(
            ProcessingAttempt.job_id == job.id,
            ProcessingAttempt.attempt_number == job.attempts + 1,
        )
        .with_for_update()
    )
    if attempt is not None and attempt.state is ProcessingState.RUNNING:
        attempt.state = ProcessingState.CANCELLED
        attempt.finished_at = finished_at
        job.attempts += 1
    job.state = ProcessingState.CANCELLED


def record_cancelled_attempt(db: Session, job: Job, emitted_at: datetime) -> None:
    """Record a late worker event once when its job was already cancelled."""
    if job.attempts:
        previous_attempt = db.scalar(
            select(ProcessingAttempt)
            .where(
                ProcessingAttempt.job_id == job.id,
                ProcessingAttempt.attempt_number == job.attempts,
            )
            .with_for_update()
        )
        if previous_attempt is not None and previous_attempt.state is ProcessingState.CANCELLED:
            return
    attempt_number = job.attempts + 1
    attempt = db.scalar(
        select(ProcessingAttempt)
        .where(
            ProcessingAttempt.job_id == job.id,
            ProcessingAttempt.attempt_number == attempt_number,
        )
        .with_for_update()
    )
    if attempt is None:
        attempt = ProcessingAttempt(
            conversation_id=job.conversation_id,
            job_id=job.id,
            kind=job.kind,
            attempt_number=attempt_number,
            state=ProcessingState.CANCELLED,
            started_at=job.created_at,
            finished_at=emitted_at,
        )
        db.add(attempt)
    else:
        if attempt.state is not ProcessingState.RUNNING:
            return
        attempt.state = ProcessingState.CANCELLED
        attempt.finished_at = emitted_at
    job.attempts += 1


def supersede_summarization(db: Session, conversation_id: UUID) -> None:
    """Cancel active summaries and drain their unpublished outbox messages."""
    now = datetime.now(timezone.utc)
    jobs = list(
        db.scalars(
            select(Job)
            .where(
                Job.conversation_id == conversation_id,
                Job.kind == ProcessingKind.SUMMARIZATION,
                Job.state.in_((ProcessingState.QUEUED, ProcessingState.RUNNING)),
            )
            .with_for_update()
        )
    )
    for job in jobs:
        cancel_job(db, job, now)

    for message in db.scalars(
        select(OutboxMessage).where(
            OutboxMessage.conversation_id == conversation_id,
            OutboxMessage.published_at.is_(None),
        )
    ):
        payload = message.payload
        if isinstance(payload, dict) and payload.get("kind") == ProcessingKind.SUMMARIZATION.value:
            message.published_at = now
