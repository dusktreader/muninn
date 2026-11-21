"""Exceptions raised while coordinating processing jobs."""

from typing import Any

from muninn.exceptions import MuninnError
from muninn.job_manager.schemas import CompletionRejectionContext


class JobManagerError(MuninnError):
    """Represent a failure owned by the job manager."""


class CompletionRejectedError(JobManagerError):
    """Represent a completion that cannot be applied to the current job state."""

    def __init__(self, message: str, *args: Any, context: CompletionRejectionContext | None = None, **kwargs: Any) -> None:
        self.context = context
        if context is not None:
            message = f"{message} [{context.describe()}]"
        super().__init__(message, *args, **kwargs)
