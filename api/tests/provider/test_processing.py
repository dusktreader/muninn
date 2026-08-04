"""Verify the fake-provider worker vertical slice with in-memory Object Store fakes."""

from uuid import uuid4
from typing import cast

import pytest
import httpx
from anyio import run

from tests.support.fake_provider import FakeProvider
from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.schemas import SummaryResult, TranscriptResult
from muninn.queue.contracts import JobMessage, ResultObject
from muninn.queue.store import JetStreamObjectStore
from muninn.config import service_settings
from muninn.exceptions import MuninnError
from muninn.provider import AssemblyAIProvider
from muninn.worker.exceptions import ProcessingError
from muninn.worker.service import Worker
from muninn.worker.utils import _map_processing_exception
from tests.support.fake_object_store import InMemoryObjectStore


class InvalidOutputProvider(FakeProvider):
    """Raise provider validation failures with unsafe exception text."""

    async def transcribe(self, audio: bytes, model: str) -> TranscriptResult:
        """Raise a representative invalid provider response."""
        raise ValueError("secret provider response body")

    async def summarize(
        self, transcript: TranscriptResult, model: str, speaker_mapping: dict[str, str] | None = None
    ) -> SummaryResult:
        """Raise a representative invalid provider response."""
        raise ValueError("secret provider response body")


class FailingObjectStore(InMemoryObjectStore):
    """Raise a selected mapped failure while reading a source object."""

    def __init__(self, exception: Exception) -> None:
        super().__init__()
        self.exception = exception

    async def get(self, reference: ResultObject) -> bytes:
        """Raise the configured storage failure."""
        raise self.exception


@pytest.mark.unit
def test_provider_from_settings_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject an AssemblyAI provider configuration without its API key."""
    monkeypatch.setattr(service_settings, "assemblyai_api_key", None)

    with pytest.raises(MuninnError, match="required"):
        AssemblyAIProvider.from_settings()


@pytest.mark.unit
@pytest.mark.parametrize("setting", ["assemblyai_transcription_model", "assemblyai_summary_model"])
def test_worker_from_dependencies_rejects_missing_required_models(monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    """Reject missing model IDs before passing them to the worker provider APIs."""
    monkeypatch.setattr(service_settings, "assemblyai_transcription_model", "transcription")
    monkeypatch.setattr(service_settings, "assemblyai_summary_model", "summary")
    monkeypatch.setattr(service_settings, setting, None)

    with pytest.raises(MuninnError, match="required"):
        Worker.from_dependencies(cast(JetStreamObjectStore, InMemoryObjectStore()), FakeProvider())


@pytest.mark.unit
def test_fake_worker_writes_transcript_result_to_object_store() -> None:
    """Process audio without SQLAlchemy, MinIO, NATS, or a live Zen service."""
    run(_test_fake_worker_writes_transcript_result)


async def _test_fake_worker_writes_transcript_result() -> None:
    store = InMemoryObjectStore()
    source = await store.put("inputs/source", b"audio", ArtifactKind.RECORDING)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        source_object=source,
    )

    event = await Worker(
        cast(JetStreamObjectStore, store), FakeProvider(), "fake-transcription", "fake-summary"
    ).process(job)

    assert event.state is ProcessingState.SUCCEEDED
    assert event.transcript_version is None
    assert event.result_object is not None
    result = TranscriptResult.model_validate_json(await store.get(event.result_object))
    assert result.language == "en"
    assert result.segments[0].speaker == "Speaker 1"


@pytest.mark.unit
def test_worker_rejects_missing_source_reference_without_ready_state() -> None:
    """Return a terminal safe failure when a malformed job lacks its NATS input reference."""
    run(_test_worker_rejects_missing_source_reference)


async def _test_worker_rejects_missing_source_reference() -> None:
    job = JobMessage(job_id=uuid4(), conversation_id=uuid4(), kind=ProcessingKind.TRANSCRIPTION, source_version=1)

    event = await Worker(
        cast(JetStreamObjectStore, InMemoryObjectStore()), FakeProvider(), "fake-transcription", "fake-summary"
    ).process(job)

    assert event.state is ProcessingState.FAILED
    assert event.error_code is ProcessingErrorCode.MISSING_INPUT
    assert event.error_details is None
    assert event.retryable is False


@pytest.mark.unit
def test_worker_uses_processing_error_payload_and_carried_job() -> None:
    """Preserve a processing error's failure data and carried job through the shared handler."""
    run(_test_worker_uses_processing_error_payload_and_carried_job)


async def _test_worker_uses_processing_error_payload_and_carried_job() -> None:
    store = FailingObjectStore(
        ProcessingError(
            "safe processing failure",
            error_code=ProcessingErrorCode.PROVIDER_QUOTA,
            retryable=True,
            details="carried processing details",
        )
    )
    source = await store.put("inputs/source", b"audio", ArtifactKind.RECORDING)
    original_job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        source_object=source,
    )
    carried_job = original_job.model_copy(update={"job_id": uuid4(), "conversation_id": uuid4()})
    setattr(store.exception, "job", carried_job)

    event = await Worker(
        cast(JetStreamObjectStore, store),
        FakeProvider(),
        "transcription",
        "summary",
    ).process(original_job)

    assert event.state is ProcessingState.FAILED
    assert event.job_id == carried_job.job_id
    assert event.conversation_id == carried_job.conversation_id
    assert event.error_code is ProcessingErrorCode.PROVIDER_QUOTA
    assert event.retryable is True
    assert event.error_details == "carried processing details"


@pytest.mark.unit
def test_worker_rejects_invalid_provider_output_with_stable_failure_event() -> None:
    """Centralize invalid provider output as a safe, non-retryable completion event."""
    run(_test_worker_rejects_invalid_provider_output)


async def _test_worker_rejects_invalid_provider_output() -> None:
    store = InMemoryObjectStore()
    source = await store.put("inputs/source", b"audio", ArtifactKind.RECORDING)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        source_object=source,
    )

    event = await Worker(
        cast(JetStreamObjectStore, store), InvalidOutputProvider(), "transcription", "summary"
    ).process(job)

    assert event.state is ProcessingState.FAILED
    assert event.error_code is ProcessingErrorCode.PROVIDER_INVALID_RESPONSE
    assert event.retryable is False
    assert event.error_details == "provider response was invalid"
    assert "secret" not in event.error_details


@pytest.mark.unit
def test_worker_rejects_invalid_source_transcript_with_stable_failure_event() -> None:
    """Centralize invalid source transcript validation as a safe completion event."""
    run(_test_worker_rejects_invalid_source_transcript)


async def _test_worker_rejects_invalid_source_transcript() -> None:
    store = InMemoryObjectStore()
    source = await store.put("inputs/source", b"secret invalid transcript", ArtifactKind.TRANSCRIPT)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        source_object=source,
    )

    event = await Worker(cast(JetStreamObjectStore, store), FakeProvider(), "transcription", "summary").process(job)

    assert event.state is ProcessingState.FAILED
    assert event.error_code is ProcessingErrorCode.INVALID_INPUT
    assert event.retryable is False
    assert event.error_details == "source transcript was invalid"
    assert "secret" not in event.error_details


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exception", "expected_code", "expected_retryable", "expected_details"),
    [
        (
            httpx.TimeoutException("secret timeout"),
            ProcessingErrorCode.PROVIDER_TIMEOUT,
            True,
            "provider request timed out",
        ),
        (
            OSError("secret storage detail"),
            ProcessingErrorCode.INFRASTRUCTURE_UNAVAILABLE,
            True,
            "processing infrastructure was unavailable",
        ),
    ],
)
def test_worker_emits_stable_events_for_mapped_failures(
    exception: Exception,
    expected_code: ProcessingErrorCode,
    expected_retryable: bool,
    expected_details: str,
) -> None:
    """Use the same failure event shape for representative infrastructure failures."""
    run(_test_worker_emits_stable_event, exception, expected_code, expected_retryable, expected_details)


async def _test_worker_emits_stable_event(
    exception: Exception,
    expected_code: ProcessingErrorCode,
    expected_retryable: bool,
    expected_details: str,
) -> None:
    store = FailingObjectStore(exception)
    source = await store.put("inputs/source", b"audio", ArtifactKind.RECORDING)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
        source_object=source,
    )

    event = await Worker(cast(JetStreamObjectStore, store), FakeProvider(), "transcription", "summary").process(job)

    assert event.state is ProcessingState.FAILED
    assert event.error_code is expected_code
    assert event.retryable is expected_retryable
    assert event.error_details == expected_details
    assert "secret" not in (event.error_details or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exception", "expected_code", "expected_retryable"),
    [
        (httpx.TimeoutException("secret timeout"), ProcessingErrorCode.PROVIDER_TIMEOUT, True),
        (httpx.RequestError("secret network detail"), ProcessingErrorCode.PROVIDER_UNAVAILABLE, True),
        (OSError("secret storage detail"), ProcessingErrorCode.INFRASTRUCTURE_UNAVAILABLE, True),
        (Exception("secret unexpected detail"), ProcessingErrorCode.UNKNOWN, True),
    ],
)
def test_worker_exception_mapping_is_stable_and_does_not_expose_messages(
    exception: Exception, expected_code: ProcessingErrorCode, expected_retryable: bool
) -> None:
    """Map representative failures to stable codes without serializing exception messages."""
    failure = _map_processing_exception(exception)

    assert failure.error_code is expected_code
    assert failure.retryable is expected_retryable
    assert failure.details is not None
    assert "secret" not in failure.details


@pytest.mark.unit
def test_worker_maps_provider_quota_without_serializing_provider_response() -> None:
    """Classify quota responses while keeping provider response bodies out of diagnostics."""
    response = httpx.Response(
        429,
        request=httpx.Request("POST", "https://provider.invalid"),
        headers={"x-request-id": "req-123"},
        content=b"secret body",
    )
    exception = httpx.HTTPStatusError("secret message", request=response.request, response=response)

    failure = _map_processing_exception(exception)

    assert failure.error_code is ProcessingErrorCode.PROVIDER_QUOTA
    assert failure.retryable is True
    assert failure.details == "provider quota was exceeded (HTTP 429, request ID req-123)"
    assert "secret" not in failure.details


@pytest.mark.unit
@pytest.mark.parametrize("request_id", ["/secret", "a" * 129])
def test_worker_omits_unsafe_provider_request_ids(request_id: str) -> None:
    request = httpx.Request("POST", "https://provider.invalid")
    response = httpx.Response(502, request=request, headers={"x-request-id": request_id})
    exception = httpx.HTTPStatusError("secret message", request=request, response=response)

    failure = _map_processing_exception(exception)

    assert failure.details == "provider service was unavailable (HTTP 502)"
    assert "secret" not in failure.details


@pytest.mark.unit
def test_fake_worker_summarizes_only_the_selected_transcript() -> None:
    """Keep summary processing on the explicit transcript object reference."""
    run(_test_fake_worker_summarizes_only_the_selected_transcript)


async def _test_fake_worker_summarizes_only_the_selected_transcript() -> None:
    store = InMemoryObjectStore()
    transcript = TranscriptResult.model_validate(
        {
            "segments": [
                {
                    "evidence_id": "evidence-1",
                    "speaker": "Speaker 1",
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "text": "Use the fake provider.",
                }
            ],
            "provider": "fake",
            "model": "fake-transcription",
        }
    )
    source = await store.put("inputs/transcript", transcript.model_dump_json().encode(), ArtifactKind.TRANSCRIPT)

    class MappingProvider(FakeProvider):
        received_mapping: dict[str, str] | None = None

        async def summarize(
            self, transcript: TranscriptResult, model: str, speaker_mapping: dict[str, str] | None = None
        ) -> SummaryResult:
            self.received_mapping = speaker_mapping
            return await super().summarize(transcript, model, speaker_mapping)

    provider = MappingProvider()
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        speaker_revision=4,
        speaker_mapping={"Speaker 1": "Ada Lovelace"},
        source_object=source,
    )

    event = await Worker(
        cast(JetStreamObjectStore, store), provider, "fake-transcription", "fake-summary"
    ).process(job)

    assert event.state is ProcessingState.SUCCEEDED
    assert event.transcript_version == job.transcript_version
    assert event.speaker_revision == job.speaker_revision
    assert provider.received_mapping == {"Speaker 1": "Ada Lovelace"}
    assert event.result_object is not None
    result = SummaryResult.model_validate_json(await store.get(event.result_object))
    assert result.overview is not None
    assert result.overview.evidence_span_ids == ["evidence-1"]


@pytest.mark.unit
def test_fake_worker_accepts_empty_summary_sections_and_nullable_overview() -> None:
    run(_test_fake_worker_accepts_empty_summary_sections_and_nullable_overview)


async def _test_fake_worker_accepts_empty_summary_sections_and_nullable_overview() -> None:
    store = InMemoryObjectStore()
    transcript = TranscriptResult(segments=[], provider="fake", model="fake-transcription")
    source = await store.put("inputs/transcript", transcript.model_dump_json().encode(), ArtifactKind.TRANSCRIPT)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        source_object=source,
    )

    event = await Worker(
        cast(JetStreamObjectStore, store), FakeProvider(), "fake-transcription", "fake-summary"
    ).process(job)

    assert event.state is ProcessingState.SUCCEEDED
    assert event.result_object is not None
    summary = SummaryResult.model_validate_json(await store.get(event.result_object))
    assert summary.overview is None
    assert summary.key_topics == []
    assert summary.decisions == []
    assert summary.action_items == []


@pytest.mark.unit
def test_worker_exposes_safe_provider_diagnostics_for_summary_failure() -> None:
    run(_test_worker_exposes_safe_provider_diagnostics_for_summary_failure)


async def _test_worker_exposes_safe_provider_diagnostics_for_summary_failure() -> None:
    store = InMemoryObjectStore()
    transcript = TranscriptResult(segments=[], provider="fake", model="fake-transcription")
    source = await store.put("inputs/transcript", transcript.model_dump_json().encode(), ArtifactKind.TRANSCRIPT)
    job = JobMessage(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=ProcessingKind.SUMMARIZATION,
        source_version=1,
        transcript_version=1,
        source_object=source,
    )
    request = httpx.Request("POST", "https://provider.invalid")
    response = httpx.Response(
        503,
        request=request,
        headers={"x-request-id": "summary-req-456"},
        content=b"secret provider response",
    )
    exception = httpx.HTTPStatusError("secret provider exception", request=request, response=response)

    class FailingSummaryProvider(FakeProvider):
        async def summarize(
        self, transcript: TranscriptResult, model: str, speaker_mapping: dict[str, str] | None = None
    ) -> SummaryResult:
            raise exception

    event = await Worker(
        cast(JetStreamObjectStore, store), FailingSummaryProvider(), "fake-transcription", "fake-summary"
    ).process(job)

    assert event.state is ProcessingState.FAILED
    assert event.error_code is ProcessingErrorCode.PROVIDER_UNAVAILABLE
    assert event.retryable is True
    assert event.error_details == "provider service was unavailable (HTTP 503, request ID summary-req-456)"
    assert "secret" not in event.error_details
