"""Verify fixed NATS subjects and queue publishing references."""

from hashlib import sha256
from typing import cast
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from anyio import run
from buzz import DoExceptParams
import pytest
from nats.js.errors import DigestMismatchError, ObjectNotFoundError, ServiceUnavailableError
from nats.js.object_store import ObjectStore

from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.queue.constants import NatsSubject
from muninn.queue.exceptions import (
    ObjectStoreError,
    ObjectStoreIntegrityError,
    ObjectStoreNotFoundError,
    ObjectStoreUnavailableError,
    QueueError,
)
from muninn.queue.contracts import CompletionEvent, JobMessage, ResultObject
from nats.js import JetStreamContext

from muninn.logging import log_buzz_error, log_error
from muninn.queue.manager import QueueManager
from muninn.queue.store import JetStreamObjectStore


@pytest.mark.unit
def test_object_store_errors_share_the_queue_error_root() -> None:
    """Keep Object Store failures inside the project queue exception hierarchy."""
    assert issubclass(ObjectStoreError, QueueError)


@pytest.mark.unit
def test_log_buzz_error_logs_the_original_exception_and_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log the Buzz callback's original exception rather than replacing its details."""
    error = RuntimeError("NATS connection failed")
    logger = Mock()
    logger.opt.return_value = logger
    monkeypatch.setattr("muninn.logging.logger", logger)

    log_buzz_error(DoExceptParams(error, "worker failed", "worker failed: NATS connection failed", None))

    logger.opt.assert_called_once_with(exception=(RuntimeError, error, None))
    logger.error.assert_called_once_with("worker failed: NATS connection failed")


@pytest.mark.unit
def test_log_error_uses_the_caught_exception_traceback_and_preserves_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Log a caught exception's type, traceback, and original message through Loguru."""
    try:
        raise RuntimeError("provider details")
    except RuntimeError as error:
        logger = Mock()
        logger.opt.return_value = logger
        monkeypatch.setattr("muninn.logging.logger", logger)

        log_error(error, "operation failed")

        logger.opt.assert_called_once_with(exception=(RuntimeError, error, error.__traceback__))
        logger.error.assert_called_once_with("operation failed")


@pytest.mark.unit
def test_log_error_binds_structured_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attach supplied context without changing exception details or the log message."""
    error = RuntimeError("provider details")
    logger = Mock()
    bound_logger = Mock()
    logger.bind.return_value = bound_logger
    bound_logger.opt.return_value = bound_logger
    monkeypatch.setattr("muninn.logging.logger", logger)

    log_error(error, "operation failed", context={"outbox_id": "outbox-1"})

    logger.bind.assert_called_once_with(outbox_id="outbox-1")
    bound_logger.opt.assert_called_once_with(exception=(RuntimeError, error, error.__traceback__))
    bound_logger.error.assert_called_once_with("operation failed")


@pytest.mark.unit
def test_nats_subjects_preserve_the_wire_values() -> None:
    """Keep the fixed subject names stable for publishers and subscriptions."""
    assert {subject.name: subject.value for subject in NatsSubject} == {
        "JOBS": "muninn.jobs",
        "COMPLETIONS": "muninn.job-completions",
        "DEAD_LETTERS": "muninn.dead-letter",
    }


@pytest.mark.unit
def test_queue_manager_injection_and_publishing_use_fixed_wire_values() -> None:
    """Build the queue manager with an injected store and publish to fixed subjects."""
    run(_test_queue_manager_injection_and_publishing)


async def _test_queue_manager_injection_and_publishing() -> None:
    jetstream = AsyncMock()
    bucket = AsyncMock()
    queue = QueueManager(
        jetstream=cast(JetStreamContext, jetstream),
        object_store=JetStreamObjectStore(cast(ObjectStore, bucket), "results"),
    )
    assert queue.jetstream is jetstream
    assert queue.object_store.bucket is bucket
    assert queue.object_store.bucket_name == "results"
    jetstream.object_store.assert_not_awaited()

    job_id = uuid4()
    job = JobMessage(job_id=job_id, conversation_id=uuid4(), kind=ProcessingKind.TRANSCRIPTION, source_version=1)
    event = CompletionEvent(
        job_id=job_id,
        conversation_id=job.conversation_id,
        kind=job.kind,
        source_version=job.source_version,
        state=ProcessingState.FAILED,
        error_code=ProcessingErrorCode.UNKNOWN,
    )

    await queue.publish_job(job)
    await queue.publish_completion(event)

    assert jetstream.publish.await_args_list[0].args[0] is NatsSubject.JOBS
    assert jetstream.publish.await_args_list[1].args[0] is NatsSubject.COMPLETIONS
    assert jetstream.publish.await_args_list[1].kwargs["headers"]["Nats-Msg-Id"] == f"{job_id}:failed"


@pytest.mark.unit
def test_queue_manager_subscriptions_use_fixed_durables() -> None:
    """Create worker and manager subscriptions with their stable durable names."""
    run(_test_queue_manager_subscriptions_use_fixed_durables)


async def _test_queue_manager_subscriptions_use_fixed_durables() -> None:
    jetstream = AsyncMock()
    bucket = AsyncMock()
    queue = QueueManager(
        jetstream=cast(JetStreamContext, jetstream),
        object_store=JetStreamObjectStore(cast(ObjectStore, bucket), "muninn"),
    )
    jetstream.object_store.assert_not_awaited()

    await queue.subscribe_jobs()
    await queue.subscribe_completions()

    assert jetstream.pull_subscribe.await_args_list[0].args[0] is NatsSubject.JOBS
    assert jetstream.pull_subscribe.await_args_list[0].kwargs == {"durable": "muninn-job-worker"}
    assert jetstream.pull_subscribe.await_args_list[1].args[0] is NatsSubject.COMPLETIONS
    assert jetstream.pull_subscribe.await_args_list[1].kwargs == {"durable": "muninn-job-manager"}


class FakeObjectResult:
    """Provide the data attribute returned by a NATS Object Store get."""

    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeBucket:
    """Raise a selected provider exception from an Object Store operation."""

    def __init__(self, error: Exception | None = None, data: bytes = b"content") -> None:
        self.error = error
        self.data = data

    async def put(self, _key: str, _content: bytes) -> None:
        if self.error is not None:
            raise self.error

    async def get(self, _key: str) -> FakeObjectResult:
        if self.error is not None:
            raise self.error
        return FakeObjectResult(self.data)


def _reference(content: bytes = b"content") -> ResultObject:
    return ResultObject(kind=ArtifactKind.RECORDING, bucket="muninn", key="object", sha256=sha256(content).hexdigest(), byte_size=len(content))


@pytest.mark.unit
def test_object_store_put_translates_provider_failure_and_preserves_cause() -> None:
    run(_test_object_store_put_translates_provider_failure)


async def _test_object_store_put_translates_provider_failure() -> None:
    cause = ServiceUnavailableError(description="provider details")
    with pytest.raises(ObjectStoreUnavailableError) as raised:
        await JetStreamObjectStore(cast(ObjectStore, FakeBucket(cause))).put("object", b"content", ArtifactKind.RECORDING)
    assert raised.value.__cause__ is cause
    assert "provider details" in str(raised.value)


@pytest.mark.unit
def test_object_store_get_translates_missing_and_integrity_failures() -> None:
    run(_test_object_store_get_translates_failures)


async def _test_object_store_get_translates_failures() -> None:
    reference = _reference()
    missing_cause = ObjectNotFoundError(description="provider details")
    with pytest.raises(ObjectStoreNotFoundError) as missing:
        await JetStreamObjectStore(cast(ObjectStore, FakeBucket(missing_cause))).get(reference)
    assert isinstance(missing.value.__cause__, ObjectNotFoundError)
    assert "provider details" in str(missing.value)

    integrity_cause = DigestMismatchError(description="provider details")
    with pytest.raises(ObjectStoreIntegrityError) as corrupt:
        await JetStreamObjectStore(cast(ObjectStore, FakeBucket(integrity_cause))).get(reference)
    assert isinstance(corrupt.value.__cause__, DigestMismatchError)
    assert "provider details" in str(corrupt.value)

    with pytest.raises(ObjectStoreIntegrityError):
        await JetStreamObjectStore(cast(ObjectStore, FakeBucket(data=b"changed"))).get(reference)


@pytest.mark.unit
def test_object_store_does_not_swallow_unexpected_provider_exceptions() -> None:
    run(_test_object_store_does_not_swallow_unexpected_provider_exceptions)


async def _test_object_store_does_not_swallow_unexpected_provider_exceptions() -> None:
    unexpected = RuntimeError("unexpected implementation failure")
    with pytest.raises(RuntimeError) as raised:
        await JetStreamObjectStore(cast(ObjectStore, FakeBucket(unexpected))).get(_reference())
    assert raised.value is unexpected
