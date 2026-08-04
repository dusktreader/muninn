"""Verify AssemblyAI request contracts and result normalization."""

import json
import traceback

import httpx
import pytest
from anyio import run

from muninn.provider import AssemblyAIProvider
from muninn.schemas import TranscriptResult


@pytest.mark.unit
def test_transcribe_uploads_audio_requests_diarization_and_normalizes_segments() -> None:
    run(_test_transcribe_uploads_audio_requests_diarization_and_normalizes_segments)


async def _test_transcribe_uploads_audio_requests_diarization_and_normalizes_segments() -> None:
    requests: list[httpx.Request] = []
    transcript_responses = iter([
        {"id": "transcript-1", "status": "queued"},
        {
            "id": "transcript-1",
            "status": "completed",
            "speech_model_used": "universal-3-5-pro",
            "language_code": "en_us",
            "utterances": [
                {"speaker": "A", "text": "Hello there.", "start": 1250, "end": 2400},
                {"speaker": "B", "text": "Hi.", "start": 2500, "end": 3100},
            ],
        },
    ])

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v2/upload":
            assert request.headers["authorization"] == "assembly-secret"
            assert request.content == b"audio-bytes"
            return httpx.Response(200, json={"upload_url": "https://cdn.assemblyai.com/upload"})
        if request.url.path == "/v2/transcript":
            payload = json.loads(request.content)
            assert payload == {
                "audio_url": "https://cdn.assemblyai.com/upload",
                "speech_models": ["universal-3-5-pro"],
                "speaker_labels": True,
                "language_code": "en_us",
            }
            return httpx.Response(200, json=next(transcript_responses))
        if request.url.path == "/v2/transcript/transcript-1":
            return httpx.Response(200, json=next(transcript_responses))
        raise AssertionError(f"unexpected request path: {request.url.path}")

    provider = AssemblyAIProvider(
        "assembly-secret",
        "universal-3-5-pro",
        "gemma-4-31b",
        transport=httpx.MockTransport(handler),
        poll_interval=0,
    )
    try:
        result = await provider.transcribe(b"audio-bytes", "universal-3-5-pro")
    finally:
        await provider.aclose()

    assert result == TranscriptResult.model_validate(
        {
            "language": "en",
            "segments": [
                {
                    "evidence_id": "segment-1",
                    "speaker": "Speaker 1",
                    "start_seconds": 1.25,
                    "end_seconds": 2.4,
                    "text": "Hello there.",
                },
                {
                    "evidence_id": "segment-2",
                    "speaker": "Speaker 2",
                    "start_seconds": 2.5,
                    "end_seconds": 3.1,
                    "text": "Hi.",
                },
            ],
            "provider": "assemblyai",
            "model": "universal-3-5-pro",
        }
    )
    assert [request.url.path for request in requests] == [
        "/v2/upload",
        "/v2/transcript",
        "/v2/transcript/transcript-1",
    ]


@pytest.mark.unit
def test_summarize_requests_strict_structured_output_for_gemma() -> None:
    run(_test_summarize_requests_strict_structured_output_for_gemma)


async def _test_summarize_requests_strict_structured_output_for_gemma() -> None:
    requests: list[httpx.Request] = []
    summary_content = json.dumps(
        {
            "overview": {
                "text": "They agreed to schedule a follow-up.",
                "evidence_span_ids": ["segment-1"],
                "confidence": "certain",
                "owner": None,
                "due_date": None,
            },
            "key_topics": [],
            "decisions": [],
            "action_items": [],
        }
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": summary_content}}]},
        )

    provider = AssemblyAIProvider(
        "assembly-secret",
        "universal-3-5-pro",
        "gemma-4-31b",
        transport=httpx.MockTransport(handler),
    )
    transcript = TranscriptResult.model_validate(
        {
            "segments": [
                {
                    "evidence_id": "segment-1",
                    "speaker": "Speaker 1",
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "text": "Let's schedule a follow-up.",
                }
            ],
            "provider": "assemblyai",
            "model": "universal-3-5-pro",
        }
    )
    try:
        result = await provider.summarize(transcript, "gemma-4-31b", {"Speaker 1": "Ada Lovelace"})
    finally:
        await provider.aclose()

    payload = json.loads(requests[0].content)
    assert requests[0].url == "https://llm-gateway.assemblyai.com/v1/chat/completions"
    assert requests[0].headers["authorization"] == "assembly-secret"
    assert payload["model"] == "gemma-4-31b"
    provider_context = json.loads(payload["messages"][1]["content"])
    assert provider_context == {
        "transcript": transcript.model_dump(mode="json"),
        "speaker_names": {"Speaker 1": "Ada Lovelace"},
    }
    assert "user-provided labels" in payload["messages"][0]["content"]
    assert payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "muninn_summary",
            "strict": True,
            "schema": AssemblyAIProvider._summary_json_schema(),
        },
    }
    assert payload["post_processing_steps"] == [{"type": "json-repair"}]
    assert payload["max_tokens"] == 2048
    assert result.provider == "assemblyai"
    assert result.model == "gemma-4-31b"
    assert result.overview is not None
    assert result.overview.evidence_span_ids == ["segment-1"]


@pytest.mark.unit
def test_summarize_accepts_null_overview_and_empty_sections() -> None:
    run(_test_summarize_accepts_null_overview_and_empty_sections)


async def _test_summarize_accepts_null_overview_and_empty_sections() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "overview": None,
                                    "key_topics": [],
                                    "decisions": [],
                                    "action_items": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = AssemblyAIProvider(
        "assembly-secret",
        "universal-3-5-pro",
        "gemma-4-31b",
        transport=httpx.MockTransport(handler),
    )
    transcript = TranscriptResult(segments=[], provider="assemblyai", model="universal-3-5-pro")
    try:
        result = await provider.summarize(transcript, "gemma-4-31b")
    finally:
        await provider.aclose()

    assert result.overview is None
    assert result.key_topics == []
    assert result.decisions == []
    assert result.action_items == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "content",
    [
        '{"overview":{"text":"sensitive transcript phrase"',
        '{"overview":{"text":"sensitive transcript phrase",',
        '"sensitive transcript phrase"',
        '{"overview":{"text":"Claim","evidence_span_ids":"sensitive transcript phrase"}}',
        json.dumps(
            {
                "overview": {"text": "sensitive transcript phrase", "evidence_span_ids": []},
                "key_topics": [],
                "decisions": [],
                "action_items": [],
            }
        ),
    ],
)
def test_summarize_safely_rejects_invalid_or_truncated_json(content: str) -> None:
    run(_test_summarize_safely_rejects_invalid_or_truncated_json, content)


async def _test_summarize_safely_rejects_invalid_or_truncated_json(content: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    provider = AssemblyAIProvider(
        "assembly-secret",
        "universal-3-5-pro",
        "gemma-4-31b",
        transport=httpx.MockTransport(handler),
    )
    transcript = TranscriptResult(segments=[], provider="assemblyai", model="universal-3-5-pro")
    try:
        with pytest.raises(ValueError) as error:
            await provider.summarize(transcript, "gemma-4-31b")
    finally:
        await provider.aclose()

    formatted_error = "".join(traceback.format_exception(error.value))
    assert "sensitive transcript phrase" not in str(error.value)
    assert "sensitive transcript phrase" not in formatted_error


@pytest.mark.unit
def test_summarize_rejects_evidence_ids_missing_from_transcript() -> None:
    run(_test_summarize_rejects_evidence_ids_missing_from_transcript)


async def _test_summarize_rejects_evidence_ids_missing_from_transcript() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "overview": {
                                        "text": "Unsupported claim.",
                                        "evidence_span_ids": ["unknown"],
                                        "confidence": "certain",
                                        "owner": None,
                                        "due_date": None,
                                    },
                                    "key_topics": [],
                                    "decisions": [],
                                    "action_items": [],
                                }
                            )
                        }
                    }
                ]
            },
        )

    provider = AssemblyAIProvider(
        "assembly-secret",
        "universal-3-5-pro",
        "gemma-4-31b",
        transport=httpx.MockTransport(handler),
    )
    transcript = TranscriptResult.model_validate(
        {
            "segments": [
                {
                    "evidence_id": "segment-1",
                    "speaker": "Speaker 1",
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "text": "A grounded statement.",
                }
            ],
            "provider": "assemblyai",
            "model": "universal-3-5-pro",
        }
    )
    try:
        with pytest.raises(ValueError, match="evidence"):
            await provider.summarize(transcript, "gemma-4-31b")
    finally:
        await provider.aclose()
