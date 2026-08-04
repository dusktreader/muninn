"""Worker processing services and stable exception mapping."""

from dataclasses import dataclass

from buzz import enforce_defined
from pydantic import ValidationError

from muninn.config import service_settings
from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.exceptions import MuninnError
from muninn.provider import ProcessingProvider
from muninn.queue.contracts import CompletionEvent, JobMessage
from muninn.queue.store import JetStreamObjectStore
from muninn.schemas import SummaryResult, TranscriptResult
from muninn.worker.exceptions import ProcessingError
from muninn.worker.utils import _failure_event, _map_processing_exception, _select_failure_job


@dataclass(slots=True)
class Worker:
    """Process validated jobs using only a provider and NATS Object Store."""

    object_store: JetStreamObjectStore
    provider: ProcessingProvider
    transcription_model: str
    summary_model: str

    @classmethod
    def from_dependencies(cls, object_store: JetStreamObjectStore, provider: ProcessingProvider) -> "Worker":
        """Build a worker from supplied dependencies and configured model IDs."""
        transcription_model = enforce_defined(
            service_settings.assemblyai_transcription_model,
            "AssemblyAI transcription model is required",
            raise_exc_class=MuninnError,
        )
        summary_model = enforce_defined(
            service_settings.assemblyai_summary_model,
            "AssemblyAI summary model is required",
            raise_exc_class=MuninnError,
        )
        return cls(object_store, provider, transcription_model, summary_model)

    async def process(self, job: JobMessage) -> CompletionEvent:
        """Read a job input, run one processing operation, and return its completion event."""
        try:
            source_object = enforce_defined(
                job.source_object,
                "processing input is missing",
                raise_exc_class=ProcessingError,
                raise_kwargs=dict(
                    error_code=ProcessingErrorCode.MISSING_INPUT,
                    retryable=False,
                ),
            )
            source = await self.object_store.get(source_object)
            if job.kind is ProcessingKind.TRANSCRIPTION:
                with ProcessingError.handle_errors(
                    "provider response was invalid",
                    handle_exc_class=(ValidationError, ValueError),
                    raise_kwargs=dict(
                        error_code=ProcessingErrorCode.PROVIDER_INVALID_RESPONSE,
                        retryable=False,
                        details="provider response was invalid",
                    ),
                ):
                    result: TranscriptResult | SummaryResult = await self.provider.transcribe(
                        source, self.transcription_model
                    )
            else:
                with ProcessingError.handle_errors(
                    "source transcript was invalid",
                    handle_exc_class=(ValidationError, ValueError),
                    raise_kwargs=dict(
                        error_code=ProcessingErrorCode.INVALID_INPUT,
                        retryable=False,
                        details="source transcript was invalid",
                    ),
                ):
                    transcript = TranscriptResult.model_validate_json(source)
                with ProcessingError.handle_errors(
                    "provider response was invalid",
                    handle_exc_class=(ValidationError, ValueError),
                    raise_kwargs=dict(
                        error_code=ProcessingErrorCode.PROVIDER_INVALID_RESPONSE,
                        retryable=False,
                        details="provider response was invalid",
                    ),
                ):
                    result = await self.provider.summarize(
                        transcript, self.summary_model, job.speaker_mapping
                    )
            content = result.model_dump_json().encode()
            kind = ArtifactKind.TRANSCRIPT if job.kind is ProcessingKind.TRANSCRIPTION else ArtifactKind.SUMMARY
            reference = await self.object_store.put(f"results/{job.job_id}.json", content, kind)
            return CompletionEvent(
                job_id=job.job_id,
                conversation_id=job.conversation_id,
                kind=job.kind,
                source_version=job.source_version,
                transcript_version=job.transcript_version,
                speaker_revision=job.speaker_revision,
                state=ProcessingState.SUCCEEDED,
                result_object=reference,
            )
        except Exception as exc:
            failure = _map_processing_exception(exc)
            return _failure_event(
                _select_failure_job(job, exc),
                failure,
            )

