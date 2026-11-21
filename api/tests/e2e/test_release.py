"""Exercise the deployed release lifecycle without evaluating model prose quality."""

import os
import struct
import time
import wave
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import httpx
import pytest


def required_environment(name: str) -> str:
    """Return one required E2E setting or fail with an actionable message."""
    value = os.getenv(name)
    if not value:
        raise AssertionError(f"{name} is required for the explicit E2E suite")
    return value


def short_wav() -> bytes:
    """Build a deterministic one-second silent WAV recording."""
    output = BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8_000)
        recording.writeframes(struct.pack("<h", 0) * 8_000)
    return output.getvalue()


@pytest.mark.e2e
def test_authenticated_capture_processing_delete_and_no_resurrection() -> None:
    """Verify the deployed process using deployment-selected AssemblyAI models."""
    base_url = required_environment("MUNINN_E2E_BASE_URL")
    token = required_environment("MUNINN_E2E_ACCESS_TOKEN")
    required_environment("MUNINN_ASSEMBLYAI_TRANSCRIPTION_MODEL")
    required_environment("MUNINN_ASSEMBLYAI_SUMMARY_MODEL")
    conversation_id = str(uuid4())
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(base_url=base_url, headers=headers, timeout=30) as client:
        health = client.get("/readyz")
        health.raise_for_status()

        created = client.post(
            "/v1/conversations",
            json={"id": conversation_id, "occurred_at": datetime.now(timezone.utc).isoformat()},
            headers={"X-Idempotency-Key": str(uuid4())},
        )
        created.raise_for_status()

        upload = client.post(
            f"/v1/conversations/{conversation_id}/recording",
            headers={
                "X-Idempotency-Key": str(uuid4()),
                "X-Content-SHA256": sha256(short_wav()).hexdigest(),
            },
            files={"recording": ("recording.wav", short_wav(), "audio/wav")},
        )
        upload.raise_for_status()

        deadline = time.monotonic() + float(os.getenv("MUNINN_E2E_PROCESSING_TIMEOUT", "300"))
        while time.monotonic() < deadline:
            projection = client.get(f"/v1/conversations/{conversation_id}")
            projection.raise_for_status()
            state = projection.json()
            if state["transcript_available"] and state["summary_available"]:
                break
            time.sleep(2)
        else:
            raise AssertionError("processing did not commit transcript and summary before the E2E timeout")

        deleted = client.delete(f"/v1/conversations/{conversation_id}", headers={"X-Idempotency-Key": str(uuid4())})
        deleted.raise_for_status()
        assert deleted.json()["code"] == "conversation_deleted"

        stale_create = client.post(
            "/v1/conversations",
            json={"id": conversation_id, "occurred_at": datetime.now(timezone.utc).isoformat()},
        )
        assert stale_create.status_code in {409, 410}
