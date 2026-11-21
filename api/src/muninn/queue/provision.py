"""Idempotently provision the NATS JetStream streams and Object Store bucket Muninn needs.

Run once per deploy by the Helm provisioning Job. Every step is create-or-update, so reruns are safe.
"""

import asyncio
import sys
import time

from loguru import logger
from nats.aio.client import Client as NATS
from nats.js import JetStreamContext
from nats.js.api import StreamConfig, StorageType
from nats.js.errors import BucketNotFoundError, NotFoundError

from muninn.config import service_settings
from muninn.queue.constants import NatsSubject


STREAM_NAMES: dict[NatsSubject, str] = {
    NatsSubject.JOBS: "MUNINN_JOBS",
    NatsSubject.COMPLETIONS: "MUNINN_JOB_COMPLETIONS",
    NatsSubject.DEAD_LETTERS: "MUNINN_DEAD_LETTER",
}

CONNECT_DEADLINE_SECONDS = 120.0
CONNECT_RETRY_INTERVAL_SECONDS = 2.0


def stream_config(subject: NatsSubject) -> StreamConfig:
    """Build the desired stream configuration for one subject."""
    return StreamConfig(
        name=STREAM_NAMES[subject],
        subjects=[subject.value],
        storage=StorageType.FILE,
    )


async def ensure_stream(js: JetStreamContext, subject: NatsSubject) -> None:
    """Create the stream for a subject, or update it when it already exists."""
    config = stream_config(subject)
    assert config.name is not None
    try:
        await js.stream_info(config.name)
    except NotFoundError:
        logger.info(f"Creating stream {config.name} for {subject.value}")
        await js.add_stream(config)
        return
    logger.info(f"Updating stream {config.name} for {subject.value}")
    await js.update_stream(config)


async def ensure_object_store(js: JetStreamContext, bucket: str) -> None:
    """Create the Object Store bucket when it is missing."""
    try:
        await js.object_store(bucket)
    except (BucketNotFoundError, NotFoundError):
        logger.info(f"Creating object store bucket {bucket}")
        await js.create_object_store(bucket=bucket)
        return
    logger.info(f"Object store bucket {bucket} already exists")


async def provision(js: JetStreamContext, bucket: str) -> None:
    """Ensure every stream and the Object Store bucket exist."""
    for subject in NatsSubject:
        await ensure_stream(js, subject)
    await ensure_object_store(js, bucket)


async def connect_with_retry(url: str) -> NATS:
    """Connect to NATS, retrying until the server is up or the deadline passes.

    On a fresh install the NATS Deployment may still be starting when the Job runs.
    """
    client = NATS()
    deadline = time.monotonic() + CONNECT_DEADLINE_SECONDS
    while True:
        try:
            await client.connect(url)
            return client
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise
            logger.warning(f"NATS not ready at {url}: {exc}; retrying")
            await asyncio.sleep(CONNECT_RETRY_INTERVAL_SECONDS)


async def run() -> None:
    """Connect to NATS and provision the topology."""
    url = str(service_settings.nats_url)
    client = await connect_with_retry(url)
    try:
        await provision(client.jetstream(), service_settings.object_store_bucket)
    finally:
        await client.drain()
    logger.info("JetStream provisioning complete")


def main() -> None:
    """Provision NATS JetStream topology and exit nonzero on failure."""
    try:
        asyncio.run(run())
    except Exception as exc:
        logger.error(f"JetStream provisioning failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
