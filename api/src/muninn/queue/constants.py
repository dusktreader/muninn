"""Constants owned by the NATS queue and outbox boundary."""

from enum import StrEnum


class NatsSubject(StrEnum):
    """Define the only subjects used by the manager and workers."""

    JOBS = "muninn.jobs"
    COMPLETIONS = "muninn.job-completions"
    DEAD_LETTERS = "muninn.dead-letter"


TRANSCRIPTION_OUTBOX_DEDUPLICATION_KEY = "transcription:{conversation_id}:{job_id}"
SUMMARY_OUTBOX_DEDUPLICATION_KEY = "summary:{conversation_id}:{source_version}:{transcript_version}:{speaker_revision}:{job_id}"
