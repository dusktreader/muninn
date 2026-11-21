"""Job manager process entrypoints."""

import asyncio

from buzz import handle_errors_async
from nats.aio.client import Client as NATS

from muninn.config import service_settings
from muninn.db import session_factory
from muninn.logging import log_buzz_error
from muninn.queue.exceptions import QueueError
from muninn.queue.manager import QueueManager
from muninn.queue.store import JetStreamObjectStore
from muninn.storage import ObjectStore
from muninn.job_manager.service import JobManager


async def run() -> None:
    """Connect the manager to NATS and run both durable loops."""
    client = NATS()
    async with handle_errors_async("job manager failed", raise_exc_class=QueueError, do_except=log_buzz_error, do_finally=client.drain):
        await client.connect(str(service_settings.nats_url))
        jetstream = client.jetstream()
        bucket = await jetstream.object_store(service_settings.object_store_bucket)
        object_store = JetStreamObjectStore(bucket, service_settings.object_store_bucket)
        queue = QueueManager(jetstream=jetstream, object_store=object_store)
        canonical_store = ObjectStore(
            str(service_settings.object_store_endpoint),
            service_settings.object_store_bucket,
            service_settings.object_store_access_key,
            service_settings.object_store_secret_key,
        )
        manager = JobManager(queue=queue, canonical_store=canonical_store, session_factory=session_factory)
        await asyncio.gather(manager.dispatch_outbox(), manager.consume_completions())


def main() -> None:
    """Start the job manager console application."""
    asyncio.run(run())
