"""Verify the idempotency mutation context's transaction semantics."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from muninn.db.idempotency import IdempotencyContext
from muninn.db.models import Base, IdempotencyRecord
from muninn.exceptions import IdempotencyKeyReuseError


@pytest.fixture
def session() -> Iterator[Session]:
    """Create an isolated SQLite session containing the idempotency table."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        yield db
    engine.dispose()


def test_context_captures_and_completes_without_committing(session: Session) -> None:
    """Write the explicit completion while leaving commit ownership to the caller."""
    key = uuid4()
    resource_id = uuid4()
    session.begin()
    with IdempotencyContext(session, "owner", "operation", key, "fingerprint") as context:
        assert not context.is_replay
        context.capture(resource_id, {"id": str(resource_id)})

    assert session.query(IdempotencyRecord).one().status == "completed"
    session.rollback()
    assert session.query(IdempotencyRecord).one_or_none() is None


def test_context_does_not_complete_on_exception(session: Session) -> None:
    """Leave a failed mutation pending and do not issue a completion update."""
    session.begin()
    with pytest.raises(RuntimeError, match="failed mutation"):
        with IdempotencyContext(session, "owner", "operation", uuid4(), "fingerprint"):
            raise RuntimeError("failed mutation")

    session.commit()
    record = session.query(IdempotencyRecord).one()
    assert record.status == "pending"
    assert record.resource_id is None


def test_context_exposes_completed_replay(session: Session) -> None:
    """Expose the stored resource and response without attempting completion again."""
    key = uuid4()
    resource_id = uuid4()
    response = {"id": str(resource_id)}
    with IdempotencyContext(session, "owner", "operation", key, "fingerprint") as context:
        context.capture(resource_id, response)
    session.commit()

    with IdempotencyContext(session, "owner", "operation", key, "fingerprint") as context:
        assert context.is_replay
        assert context.resource_id == resource_id
        assert context.response_data == response


def test_context_rejects_key_reuse_with_different_fingerprint(session: Session) -> None:
    """Preserve the existing key-mismatch error through context entry."""
    key = uuid4()
    with IdempotencyContext(session, "owner", "operation", key, "first") as context:
        context.capture(uuid4())
    session.commit()

    with pytest.raises(IdempotencyKeyReuseError):
        with IdempotencyContext(session, "owner", "operation", key, "different"):
            pass
