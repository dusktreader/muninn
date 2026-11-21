"""Test normalized provider and queue contracts independently of model quality."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from muninn.schemas import SummaryItem
from muninn.schemas import SummaryResult, TranscriptResult, TranscriptSegment
from muninn.api.constants import UploadState
from muninn.constants import ArtifactKind, AuthorKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.queue.contracts import CompletionEvent, JobMessage, OutboxJobPayload, ResultObject


@pytest.mark.unit
def test_transcript_requires_english_numbered_speakers_and_ordered_times() -> None:
    segment = TranscriptSegment(
        evidence_id="span-1", speaker="Speaker 1", start_seconds=0, end_seconds=1, text="Hello"
    )
    result = TranscriptResult(segments=[segment], provider="zen", model="free-transcription")
    assert result.language == "en"

    with pytest.raises(ValidationError):
        TranscriptResult.model_validate(
            {"language": "fr", "segments": [segment], "provider": "zen", "model": "free-transcription"}
        )
    with pytest.raises(ValidationError):
        TranscriptSegment(evidence_id="span-2", speaker="Alice", start_seconds=0, end_seconds=1, text="Hello")
    with pytest.raises(ValidationError):
        TranscriptSegment(
            evidence_id="span-3", speaker="Speaker 2", start_seconds=2, end_seconds=1, text="Hello"
        )


@pytest.mark.unit
def test_summary_schema_preserves_four_fields_and_uncertainty() -> None:
    uncertain = SummaryItem(
        text="A decision may have been made",
        evidence_span_ids=["span-1"],
        confidence="uncertain",
    )
    result = SummaryResult(
        overview=None,
        key_topics=[],
        decisions=[uncertain],
        action_items=[],
        provider="zen",
        model="free-summary",
    )
    assert result.model_dump()["overview"] is None
    assert result.decisions[0].confidence == "uncertain"


@pytest.mark.unit
@pytest.mark.parametrize("evidence_span_ids", [[], [""], ["   "]])
def test_summary_claim_requires_nonempty_evidence_ids(evidence_span_ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        SummaryItem(text="A claim", evidence_span_ids=evidence_span_ids)


@pytest.mark.unit
def test_queue_contract_requires_result_on_success_and_error_on_failure() -> None:
    job_id = uuid4()
    conversation_id = uuid4()
    job = JobMessage(
        job_id=job_id,
        conversation_id=conversation_id,
        kind=ProcessingKind.TRANSCRIPTION,
        source_version=1,
    )
    result = ResultObject(kind=ArtifactKind.TRANSCRIPT, bucket="results", key="result.json", sha256="a" * 64, byte_size=10)
    assert CompletionEvent(
        job_id=job.job_id,
        conversation_id=job.conversation_id,
        kind=job.kind,
        source_version=job.source_version,
        state=ProcessingState.SUCCEEDED,
        result_object=result,
    ).result_object == result
    with pytest.raises(ValidationError):
        CompletionEvent(
            job_id=job.job_id,
            conversation_id=job.conversation_id,
            kind=job.kind,
            source_version=job.source_version,
            state=ProcessingState.FAILED,
        )


@pytest.mark.unit
def test_queue_contract_rejects_unconstrained_processing_error_codes() -> None:
    with pytest.raises(ValidationError):
        CompletionEvent.model_validate(
            {
                "job_id": uuid4(),
                "conversation_id": uuid4(),
                "kind": ProcessingKind.TRANSCRIPTION,
                "source_version": 1,
                "state": ProcessingState.FAILED,
                "error_code": "provider_exception",
            }
        )

    assert ProcessingErrorCode.UNKNOWN.value == "unknown"


@pytest.mark.unit
def test_author_kind_has_only_human_and_agent_wire_values() -> None:
    """Keep author-kind values constrained at the shared contract boundary."""
    assert [kind.value for kind in AuthorKind] == ["human", "agent"]


@pytest.mark.unit
def test_upload_state_has_only_supported_wire_values() -> None:
    """Keep upload lifecycle values limited to the states currently implemented."""
    assert [state.value for state in UploadState] == ["uploading", "committed"]


@pytest.mark.unit
@pytest.mark.parametrize("kind, transcript_version", [(ProcessingKind.TRANSCRIPTION, None), (ProcessingKind.SUMMARIZATION, 1)])
def test_outbox_job_payload_accepts_transcription_and_summary_payloads(
    kind: ProcessingKind, transcript_version: int | None
) -> None:
    """Accept the two current outbox payload shapes and preserve their wire values."""
    payload = OutboxJobPayload(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=kind,
        source_version=1,
        transcript_version=transcript_version,
        source_key="conversations/example/source.json",
    )

    wire_payload = payload.model_dump(mode="json")
    assert wire_payload["kind"] == kind.value
    assert OutboxJobPayload.model_validate(wire_payload) == payload


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"source_key": None},
        {"source_version": "1"},
        {"missing": None},
    ],
)
def test_outbox_job_payload_rejects_missing_and_wrong_typed_fields(change: dict[str, object]) -> None:
    """Reject incomplete or incorrectly typed outbox values before dispatch."""
    payload = {
        "job_id": uuid4(),
        "conversation_id": uuid4(),
        "kind": ProcessingKind.TRANSCRIPTION,
        "source_version": 1,
        "source_key": "recording.wav",
    }
    if "missing" in change:
        del payload["source_key"]
    else:
        payload.update(change)

    with pytest.raises(ValidationError):
        OutboxJobPayload.model_validate(payload)


@pytest.mark.unit
def test_outbox_job_payload_rejects_extra_fields() -> None:
    """Reject fields not included in the persisted outbox contract."""
    with pytest.raises(ValidationError):
        OutboxJobPayload.model_validate(
            {
                "job_id": uuid4(),
                "conversation_id": uuid4(),
                "kind": ProcessingKind.TRANSCRIPTION,
                "source_version": 1,
                "source_key": "recording.wav",
                "unexpected": True,
            }
        )
