"""Verify explicit dispositions for malformed and completion messages."""

from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from anyio import run

from muninn.constants import ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.job_manager import CompletionRejectedError, JobManager
from muninn.queue.contracts import CompletionEvent
from muninn.queue.exceptions import ObjectStoreUnavailableError
from muninn.queue.manager import QueueManager


class Message:
    """Provide the JetStream message operations used by a consumer."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ack = AsyncMock()
        self.nak = AsyncMock()
        self.term = AsyncMock()


class Subscription:
    """Return one batch, then stop a consumer test loop."""

    def __init__(self, message: Message) -> None:
        self.fetch = AsyncMock(side_effect=[[message], RuntimeError("stop test loop")])


def valid_payload() -> bytes:
    """Build a parseable completion payload for consumer tests."""
    return CompletionEvent(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        state=ProcessingState.FAILED,
        error_code=ProcessingErrorCode.UNKNOWN,
    ).model_dump_json().encode()


@pytest.mark.unit
def test_queue_manager_dead_letters_original_payload_and_reason() -> None:
    run(_test_queue_manager_dead_letters_original_payload_and_reason)


async def _test_queue_manager_dead_letters_original_payload_and_reason() -> None:
    jetstream = AsyncMock()
    queue = QueueManager(jetstream=jetstream, object_store=Mock())

    await queue.publish_dead_letter(b"original", "malformed_completion")

    jetstream.publish.assert_awaited_once_with(
        "muninn.dead-letter",
        b"original",
        headers={"Muninn-Dead-Letter-Reason": "malformed_completion"},
    )


@pytest.mark.unit
def test_malformed_completion_is_logged_dead_lettered_and_terminated() -> None:
    run(_test_malformed_completion_is_logged_dead_lettered_and_terminated)


async def _test_malformed_completion_is_logged_dead_lettered_and_terminated() -> None:
    message = Message(b"not-json")
    queue = Mock()
    queue.subscribe_completions = AsyncMock(return_value=Subscription(message))
    queue.publish_dead_letter = AsyncMock()
    manager = JobManager(queue=queue, canonical_store=Mock(), session_factory=Mock())

    with pytest.raises(RuntimeError, match="stop test loop"):
        await manager.consume_completions()

    queue.publish_dead_letter.assert_awaited_once_with(b"not-json", "malformed_completion")
    message.term.assert_awaited_once_with()
    message.ack.assert_not_awaited()


@pytest.mark.unit
def test_transient_completion_object_store_failure_is_naked_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    run(_test_transient_completion_object_store_failure_is_naked_for_retry, monkeypatch)


async def _test_transient_completion_object_store_failure_is_naked_for_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    message = Message(valid_payload())
    queue = Mock()
    queue.subscribe_completions = AsyncMock(return_value=Subscription(message))
    queue.publish_dead_letter = AsyncMock()

    async def fail(*_: object) -> bool:
        raise ObjectStoreUnavailableError("temporary transport failure")

    monkeypatch.setattr(JobManager, "apply_completion", fail)
    manager = JobManager(queue=queue, canonical_store=Mock(), session_factory=Mock())
    with pytest.raises(RuntimeError, match="stop test loop"):
        await manager.consume_completions()

    message.nak.assert_awaited_once_with()
    message.ack.assert_not_awaited()
    queue.publish_dead_letter.assert_not_awaited()


@pytest.mark.unit
def test_completion_is_terminated_when_application_rejects_it(monkeypatch: pytest.MonkeyPatch) -> None:
    run(_test_completion_is_terminated_when_application_rejects_it, monkeypatch)


async def _test_completion_is_terminated_when_application_rejects_it(monkeypatch: pytest.MonkeyPatch) -> None:
    message = Message(valid_payload())
    queue = Mock()
    queue.subscribe_completions = AsyncMock(return_value=Subscription(message))
    queue.publish_dead_letter = AsyncMock()

    async def reject(*_: object) -> None:
        raise CompletionRejectedError("completion rejected: test rejection")

    monkeypatch.setattr(JobManager, "apply_completion", reject)
    manager = JobManager(queue=queue, canonical_store=Mock(), session_factory=Mock())
    with pytest.raises(RuntimeError, match="stop test loop"):
        await manager.consume_completions()

    queue.publish_dead_letter.assert_awaited_once_with(message.data, "completion_rejected")
    message.term.assert_awaited_once_with()
    message.ack.assert_not_awaited()


@pytest.mark.unit
def test_completion_is_acked_only_after_application_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    run(_test_completion_is_acked_only_after_application_succeeds, monkeypatch)


async def _test_completion_is_acked_only_after_application_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    message = Message(valid_payload())
    queue = Mock()
    queue.subscribe_completions = AsyncMock(return_value=Subscription(message))
    queue.publish_dead_letter = AsyncMock()

    async def succeed(*_: object) -> None:
        return None

    monkeypatch.setattr(JobManager, "apply_completion", succeed)
    manager = JobManager(queue=queue, canonical_store=Mock(), session_factory=Mock())
    with pytest.raises(RuntimeError, match="stop test loop"):
        await manager.consume_completions()

    message.ack.assert_awaited_once_with()
    message.nak.assert_not_awaited()
    message.term.assert_not_awaited()
