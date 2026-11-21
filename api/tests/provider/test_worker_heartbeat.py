"""Verify workers extend JetStream message leases during long processing."""

import asyncio
import importlib
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from anyio import run

from muninn.constants import ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.queue.contracts import CompletionEvent, JobMessage
from muninn.worker.main import process_jobs


class Message:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.in_progress = AsyncMock()
        self.ack = AsyncMock()
        self.nak = AsyncMock()
        self.term = AsyncMock()


class Subscription:
    def __init__(self, message: Message) -> None:
        self.fetch = AsyncMock(side_effect=[[message], RuntimeError("stop test loop")])


@pytest.mark.unit
def test_worker_extends_jetstream_lease_while_provider_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    run(_test_worker_extends_jetstream_lease_while_provider_job_runs, monkeypatch)


async def _test_worker_extends_jetstream_lease_while_provider_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    worker_module = importlib.import_module("muninn.worker.main")
    monkeypatch.setattr(worker_module, "JOB_HEARTBEAT_INTERVAL_SECONDS", 0.001)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
    )
    message = Message(job.model_dump_json().encode())
    queue = Mock()
    queue.subscribe_jobs = AsyncMock(return_value=Subscription(message))
    queue.publish_completion = AsyncMock()
    worker = Mock()

    async def process(_: JobMessage) -> CompletionEvent:
        await asyncio.sleep(0.01)
        return CompletionEvent(
            job_id=job.job_id,
            conversation_id=job.conversation_id,
            kind=job.kind,
            source_version=job.source_version,
            state=ProcessingState.FAILED,
            error_code=ProcessingErrorCode.UNKNOWN,
        )

    worker.process = process

    with pytest.raises(RuntimeError, match="stop test loop"):
        await process_jobs(queue, worker)

    message.in_progress.assert_awaited()
    message.ack.assert_awaited_once_with()
    assert queue.publish_completion.await_count == 2
    assert queue.publish_completion.await_args_list[0].args[0].state is ProcessingState.RUNNING
