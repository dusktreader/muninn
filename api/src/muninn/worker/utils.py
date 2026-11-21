"""Worker failure handling helpers."""

import asyncio
import re

import httpx
from pydantic import ValidationError

from muninn.constants import ProcessingErrorCode, ProcessingState
from muninn.queue.contracts import CompletionEvent, JobMessage
from muninn.queue.exceptions import (
    ObjectStoreIntegrityError,
    ObjectStoreNotFoundError,
    ObjectStoreOperationError,
    ObjectStoreUnavailableError,
)
from muninn.worker.exceptions import ProcessingError
from muninn.worker.schemas import _ProcessingFailure


def _provider_http_details(details: str, exc: httpx.HTTPStatusError) -> str:
    status_code = exc.response.status_code
    request_id = exc.response.headers.get("x-request-id", "")
    diagnostic = f"{details} (HTTP {status_code}"
    if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id, flags=re.ASCII):
        diagnostic += f", request ID {request_id}"
    return f"{diagnostic})"


def _map_processing_exception(exc: Exception) -> _ProcessingFailure:
    """Map provider and infrastructure exceptions without exposing exception data."""
    if isinstance(exc, ProcessingError):
        return _ProcessingFailure(error_code=exc.error_code, retryable=exc.retryable, details=exc.details)
    if isinstance(exc, ObjectStoreUnavailableError):
        return _ProcessingFailure(
            ProcessingErrorCode.INFRASTRUCTURE_UNAVAILABLE, True, "processing infrastructure was unavailable"
        )
    if isinstance(exc, ObjectStoreNotFoundError):
        return _ProcessingFailure(ProcessingErrorCode.MISSING_INPUT, False, "processing input was not found")
    if isinstance(exc, ObjectStoreIntegrityError):
        return _ProcessingFailure(
            ProcessingErrorCode.INFRASTRUCTURE_UNAVAILABLE, True, "processing infrastructure was unavailable"
        )
    if isinstance(exc, ObjectStoreOperationError):
        return _ProcessingFailure(ProcessingErrorCode.INVALID_INPUT, False, "processing input was invalid")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return _ProcessingFailure(ProcessingErrorCode.PROVIDER_TIMEOUT, True, "provider request timed out")
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 408:
            error_code, retryable, details = (
                ProcessingErrorCode.PROVIDER_TIMEOUT,
                True,
                "provider request timed out",
            )
        elif status_code == 429:
            error_code, retryable, details = ProcessingErrorCode.PROVIDER_QUOTA, True, "provider quota was exceeded"
        elif status_code in (401, 403):
            error_code, retryable, details = (
                ProcessingErrorCode.INVALID_CONFIGURATION,
                False,
                "provider configuration was rejected",
            )
        elif status_code == 404:
            error_code, retryable, details = ProcessingErrorCode.INVALID_MODEL, False, "provider model was not found"
        elif 400 <= status_code < 500:
            error_code, retryable, details = ProcessingErrorCode.INVALID_INPUT, False, "provider rejected the input"
        else:
            error_code, retryable, details = (
                ProcessingErrorCode.PROVIDER_UNAVAILABLE,
                True,
                "provider service was unavailable",
            )
        return _ProcessingFailure(error_code, retryable, _provider_http_details(details, exc))
    if isinstance(exc, httpx.RequestError):
        return _ProcessingFailure(ProcessingErrorCode.PROVIDER_UNAVAILABLE, True, "provider service was unavailable")
    if isinstance(exc, (OSError, RuntimeError)):
        return _ProcessingFailure(
            ProcessingErrorCode.INFRASTRUCTURE_UNAVAILABLE, True, "processing infrastructure was unavailable"
        )
    if isinstance(exc, (KeyError, ValueError, ValidationError)):
        return _ProcessingFailure(ProcessingErrorCode.INVALID_INPUT, False, "processing input was invalid")
    return _ProcessingFailure(ProcessingErrorCode.UNKNOWN, True, "unexpected processing failure")


def _select_failure_job(job: JobMessage, exc: Exception) -> JobMessage:
    """Use a job carried by a processing error when one is available."""
    if isinstance(exc, ProcessingError):
        carried_job = getattr(exc, "job", None)
        if isinstance(carried_job, JobMessage):
            return carried_job
    return job


def _failure_event(job: JobMessage, failure: _ProcessingFailure) -> CompletionEvent:
    """Build a failed completion event with the stable processing failure contract."""
    return CompletionEvent(
        job_id=job.job_id,
        conversation_id=job.conversation_id,
        kind=job.kind,
        source_version=job.source_version,
        transcript_version=job.transcript_version,
        speaker_revision=job.speaker_revision,
        state=ProcessingState.FAILED,
        error_code=failure.error_code,
        error_details=failure.details,
        retryable=failure.retryable,
    )
