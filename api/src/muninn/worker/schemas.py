"""Worker-owned data schemas."""

from dataclasses import dataclass

from muninn.constants import ProcessingErrorCode


@dataclass(frozen=True, slots=True)
class _ProcessingFailure:
    """Carry the stable data for a failed processing operation."""

    error_code: ProcessingErrorCode
    retryable: bool
    details: str | None
