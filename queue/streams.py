"""Describe durable idempotent Redis Streams jobs."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class ProcessingJob:
    """Identify one stage/version processing request."""

    job_id: UUID
    conversation_id: UUID
    stage: str
    source_version: str


class StreamQueue:
    """Provide the service-facing queue contract without exposing Redis to clients."""

    def __init__(self, stream: str = "muninn.processing") -> None:
        self.stream = stream

    @staticmethod
    def idempotency_key(job: ProcessingJob) -> str:
        """Return the stable key used to deduplicate acknowledgements."""
        return f"{job.conversation_id}:{job.stage}:{job.source_version}"
