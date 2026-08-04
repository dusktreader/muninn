"""Deterministic provider used only by tests."""

import hashlib

from muninn.schemas import SummaryItem, SummaryResult, TranscriptResult, TranscriptSegment


class FakeProvider:
    """Return stable English provider output without contacting a live service."""

    async def transcribe(self, audio: bytes, model: str) -> TranscriptResult:
        """Create one deterministic transcript segment from audio bytes."""
        digest = hashlib.sha256(audio).hexdigest()[:16]
        return TranscriptResult(
            segments=[
                TranscriptSegment(
                    evidence_id=f"evidence-{digest}",
                    speaker="Speaker 1",
                    start_seconds=0,
                    end_seconds=1,
                    text=f"Recording {digest}",
                )
            ],
            provider="fake",
            model=model,
        )

    async def summarize(
        self, transcript: TranscriptResult, model: str, speaker_mapping: dict[str, str] | None = None
    ) -> SummaryResult:
        """Create a grounded summary using only transcript evidence."""
        item = None
        if transcript.segments:
            evidence_id = transcript.segments[0].evidence_id
            item = SummaryItem(text=transcript.segments[0].text, evidence_span_ids=[evidence_id])
        return SummaryResult(overview=item, key_topics=[], decisions=[], action_items=[], provider="fake", model=model)

    async def aclose(self) -> None:
        """Do nothing because the fake does not own external clients."""
