"""Worker process entrypoints."""

import asyncio
from contextlib import suppress
from typing import Any

from buzz import handle_errors_async
from nats.aio.client import Client as NATS

from muninn.config import service_settings
from muninn.constants import ProcessingState
from muninn.logging import log_buzz_error, log_error
from muninn.provider import AssemblyAIProvider, FakeProcessingProvider
from muninn.queue.contracts import CompletionEvent, JobMessage
from muninn.queue.exceptions import ObjectStoreUnavailableError, ObjectStoreUnavailableErrors, QueueError
from muninn.queue.manager import CONSUMER_IDLE_INTERVAL_SECONDS, SUBSCRIPTION_FETCH_TIMEOUT_SECONDS, QueueManager
from muninn.queue.store import JetStreamObjectStore
from muninn.worker.service import Worker


JOB_HEARTBEAT_INTERVAL_SECONDS = 10


async def _process_with_heartbeat(message: Any, worker: Worker, job: JobMessage) -> CompletionEvent:
    """Extend a job message lease while its provider request is active."""
    process_task = asyncio.create_task(worker.process(job))
    try:
        while True:
            done, _ = await asyncio.wait({process_task}, timeout=JOB_HEARTBEAT_INTERVAL_SECONDS)
            if done:
                return await process_task
            await message.in_progress()
    except BaseException:
        process_task.cancel()
        with suppress(BaseException):
            await process_task
        raise


async def process_jobs(queue: QueueManager, worker: Worker) -> None:
    """Consume jobs, publish completion events, and dead-letter malformed messages."""
    subscription = await queue.subscribe_jobs()
    while True:
        try:
            messages = await subscription.fetch(1, timeout=SUBSCRIPTION_FETCH_TIMEOUT_SECONDS)
        except TimeoutError:
            await asyncio.sleep(CONSUMER_IDLE_INTERVAL_SECONDS)
            continue

        for message in messages:
            try:
                job = JobMessage.model_validate_json(message.data)
            except ValueError as exc:
                log_error(
                    exc,
                    "Malformed job message; dead-lettering it",
                    context={"subject": message.subject, "payload_size": len(message.data)},
                )
                await queue.publish_dead_letter(message.data, "malformed_job")
                await message.term()
                continue

            await queue.publish_completion(
                CompletionEvent(
                    job_id=job.job_id,
                    conversation_id=job.conversation_id,
                    kind=job.kind,
                    source_version=job.source_version,
                    transcript_version=job.transcript_version,
                    speaker_revision=job.speaker_revision,
                    state=ProcessingState.RUNNING,
                )
            )
            event = await _process_with_heartbeat(message, worker, job)
            try:
                await queue.publish_completion(event)
            except (ObjectStoreUnavailableError, *ObjectStoreUnavailableErrors) as exc:
                log_error(
                    exc,
                    "Transient queue failure while publishing completion; NAKing for retry",
                    context={
                        "job_id": str(job.job_id),
                        "conversation_id": str(job.conversation_id),
                        "processing_kind": job.kind.value,
                    },
                )
                await message.nak()
                continue
            except Exception as exc:
                log_error(
                    exc,
                    "Queue failure while publishing completion; leaving message unacknowledged",
                    context={
                        "job_id": str(job.job_id),
                        "conversation_id": str(job.conversation_id),
                        "processing_kind": job.kind.value,
                    },
                )
                raise

            await message.ack()


async def run() -> None:
    """Connect a worker to the scoped NATS account and process jobs."""
    client = NATS()
    async with handle_errors_async(
        "worker failed", raise_exc_class=QueueError, do_except=log_buzz_error, do_finally=client.drain
    ):
        await client.connect(str(service_settings.nats_url))
        jetstream = client.jetstream()
        bucket = await jetstream.object_store(service_settings.object_store_bucket)
        object_store = JetStreamObjectStore(bucket, service_settings.object_store_bucket)
        queue = QueueManager(jetstream=jetstream, object_store=object_store)
        provider = FakeProcessingProvider() if service_settings.provider_mode == "fake" else AssemblyAIProvider.from_settings()
        try:
            await process_jobs(queue, Worker.from_dependencies(object_store, provider))
        finally:
            await provider.aclose()


def main() -> None:
    """Start the worker console application."""
    asyncio.run(run())
