"""Manage NATS JetStream queues with explicit subject boundaries."""

from dataclasses import dataclass
from typing import ClassVar

from nats.js import JetStreamContext
from nats.js.api import Header

from muninn.queue.constants import NatsSubject
from muninn.queue.contracts import CompletionEvent, JobMessage
from muninn.queue.store import JetStreamObjectStore


OUTBOX_DISPATCH_INTERVAL_SECONDS = 0.5
CONSUMER_IDLE_INTERVAL_SECONDS = 0.1
SUBSCRIPTION_FETCH_TIMEOUT_SECONDS = 1


@dataclass(slots=True)
class QueueManager:
    """Own the JetStream subjects, consumers, and object store used by Muninn."""

    jetstream: JetStreamContext
    object_store: JetStreamObjectStore

    JOBS_SUBJECT: ClassVar[NatsSubject] = NatsSubject.JOBS
    COMPLETIONS_SUBJECT: ClassVar[NatsSubject] = NatsSubject.COMPLETIONS
    DEAD_LETTERS_SUBJECT: ClassVar[NatsSubject] = NatsSubject.DEAD_LETTERS
    JOBS_DURABLE: ClassVar[str] = "muninn-job-worker"
    COMPLETIONS_DURABLE: ClassVar[str] = "muninn-job-manager"

    async def publish_job(self, message: JobMessage) -> None:
        """Publish a validated job with its stable deduplication header."""
        await self.jetstream.publish(
            self.JOBS_SUBJECT,
            message.model_dump_json().encode(),
            headers={Header.MSG_ID: str(message.job_id)},
        )

    async def publish_completion(self, event: CompletionEvent) -> None:
        """Publish a validated completion with its stable deduplication header."""
        await self.jetstream.publish(
            self.COMPLETIONS_SUBJECT,
            event.model_dump_json().encode(),
            headers={Header.MSG_ID: f"{event.job_id}:{event.state.value}"},
        )

    async def publish_dead_letter(self, payload: bytes, reason: str) -> None:
        """Publish the original message bytes with a stable disposition reason."""
        await self.jetstream.publish(
            self.DEAD_LETTERS_SUBJECT,
            payload,
            headers={"Muninn-Dead-Letter-Reason": reason},
        )

    async def subscribe_jobs(self):
        """Create the durable pull subscription used by workers."""
        return await self.jetstream.pull_subscribe(self.JOBS_SUBJECT, durable=self.JOBS_DURABLE)

    async def subscribe_completions(self):
        """Create the durable pull subscription used by the job manager."""
        return await self.jetstream.pull_subscribe(self.COMPLETIONS_SUBJECT, durable=self.COMPLETIONS_DURABLE)
