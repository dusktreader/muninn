"""Transactional helpers for durable, caller-scoped mutation replay."""

from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from muninn.db.models import IdempotencyRecord
from muninn.exceptions import IdempotencyKeyReuseError
from muninn.schemas import IdempotencyKey


def find_idempotency_record(
    db: Session, owner_subject: str, operation_kind: str, key: IdempotencyKey, fingerprint: str
) -> IdempotencyRecord | None:
    """Find a caller-owned record and reject reuse with a different request."""
    canonical_key = str(key)
    record = (
        db.query(IdempotencyRecord)
        .filter_by(owner_subject=owner_subject, operation_kind=operation_kind, idempotency_key=canonical_key)
        .first()
    )
    IdempotencyKeyReuseError.require_condition(
        record is None or record.request_fingerprint == fingerprint,
        "Idempotency key was already used for a different request",
    )
    return record


def reserve_idempotency(
    db: Session, owner_subject: str, operation_kind: str, key: IdempotencyKey, fingerprint: str
) -> IdempotencyRecord:
    """Reserve a key before mutation work, using the database uniqueness race as the lock."""
    record = find_idempotency_record(db, owner_subject, operation_kind, key, fingerprint)
    if record is not None:
        return record

    candidate = IdempotencyRecord(
        owner_subject=owner_subject,
        operation_kind=operation_kind,
        idempotency_key=str(key),
        request_fingerprint=fingerprint,
        status="pending",
    )
    try:
        # Keep a losing uniqueness insert from invalidating the caller's
        # transaction.  The savepoint is deliberately limited to this race;
        # ownership of the outer transaction remains with the caller.
        with db.begin_nested():
            db.add(candidate)
            db.flush()
    except IntegrityError:
        record = find_idempotency_record(db, owner_subject, operation_kind, key, fingerprint)
        if record is None:
            raise
        return record
    return candidate


class IdempotencyContext:
    """Reserve an idempotency key and complete it around one mutation.

    The context manager never commits or rolls back the supplied session.  A
    caller must explicitly capture the created resource and replay response
    with `capture`; successful exit then writes the completion update.  An
    exception leaves the record pending in the caller's transaction and does
    not write a completion.
    """

    def __init__(
        self,
        db: Session,
        owner_subject: str,
        operation_kind: str,
        key: IdempotencyKey,
        fingerprint: str,
    ) -> None:
        self.db = db
        self.owner_subject = owner_subject
        self.operation_kind = operation_kind
        self.key = key
        self.fingerprint = fingerprint
        self.record: IdempotencyRecord | None = None
        self._captured: tuple[UUID | None, dict[str, Any] | None] | None = None

    def __enter__(self) -> Self:
        """Reserve the key and expose its current record to the caller."""
        self.record = reserve_idempotency(
            self.db, self.owner_subject, self.operation_kind, self.key, self.fingerprint
        )
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Complete a successful mutation without changing transaction ownership."""
        if exception_type is not None or self.is_replay:
            return None
        if self._captured is None:
            raise RuntimeError("An idempotency mutation must capture its result before exit")
        resource_id, response_data = self._captured
        assert self.record is not None
        complete_idempotency(self.db, self.record, resource_id, response_data)
        return None

    @property
    def is_replay(self) -> bool:
        """Return whether the reserved record already contains a completed result."""
        return self.record is not None and self.record.status == "completed"

    @property
    def resource_id(self) -> UUID | None:
        """Return the resource ID stored on the reserved record, if any."""
        return self.record.resource_id if self.record is not None else None

    @property
    def response_data(self) -> dict[str, Any] | None:
        """Return the replay response stored on the reserved record, if any."""
        return self.record.response_data if self.record is not None else None

    def capture(self, resource_id: UUID | None, response_data: dict[str, Any] | None = None) -> None:
        """Capture the mutation result for completion when the scope exits."""
        if self.record is None:
            raise RuntimeError("The idempotency context must be entered before capture")
        if self.is_replay:
            raise RuntimeError("A completed idempotency record cannot be captured")
        if self._captured is not None:
            raise RuntimeError("An idempotency result can only be captured once")
        self._captured = (resource_id, response_data)


def complete_idempotency(
    db: Session,
    record: IdempotencyRecord,
    resource_id: UUID | None,
    response_data: dict[str, Any] | None,
) -> None:
    """Finalize a reserved record with only bounded, replay-safe result data."""
    db.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.id == record.id)
        .values(
            resource_id=resource_id,
            response_data=response_data,
            status="completed",
            completed_at=datetime.now(timezone.utc),
        )
    )
