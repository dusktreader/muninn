"""Exceptions raised by worker processing operations."""

from muninn.constants import ProcessingErrorCode
from muninn.exceptions import MuninnError


class WorkerError(MuninnError):
    """Represent a failure owned by the processing worker."""


class ProcessingError(WorkerError):
    """Carry stable processing failure data from worker operations to their event handler."""

    def __init__(
        self,
        message: str,
        *,
        error_code: ProcessingErrorCode,
        retryable: bool,
        details: str | None = None,
        base_message: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.retryable = retryable
        self.details = details
        super().__init__(message, base_message=base_message)
