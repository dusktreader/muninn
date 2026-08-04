"""Verify constants remain owned by the package that defines their contract."""

import muninn.constants as shared_constants
import pytest
from muninn.api.constants import ALLOWED_RECORDING_TYPES, MAX_RECORDING_BYTES, UploadState
from muninn.mcp.constants import MAX_MCP_CONTEXT_BYTES
from muninn.queue.constants import NatsSubject, SUMMARY_OUTBOX_DEDUPLICATION_KEY
from muninn.storage import ArtifactContentType, CONVERSATION_ARTIFACT_KEY

pytestmark = pytest.mark.unit


def test_shared_constants_do_not_retain_package_local_values() -> None:
    """Keep API, queue, MCP, and storage implementation details out of shared vocabulary."""
    for name in (
        "ALLOWED_RECORDING_TYPES",
        "MAX_RECORDING_BYTES",
        "NatsSubject",
        "SUMMARY_OUTBOX_DEDUPLICATION_KEY",
        "MAX_MCP_CONTEXT_BYTES",
        "UploadState",
        "ArtifactContentType",
        "CONVERSATION_ARTIFACT_KEY",
    ):
        assert not hasattr(shared_constants, name)


def test_package_local_constants_preserve_existing_values() -> None:
    """Preserve the externally meaningful values while changing their import ownership."""
    assert MAX_RECORDING_BYTES == 512 * 1024 * 1024
    assert "audio/wav" in ALLOWED_RECORDING_TYPES
    assert UploadState.COMMITTED.value == "committed"
    assert NatsSubject.JOBS.value == "muninn.jobs"
    assert SUMMARY_OUTBOX_DEDUPLICATION_KEY == "summary:{conversation_id}:{source_version}:{transcript_version}:{speaker_revision}:{job_id}"
    assert MAX_MCP_CONTEXT_BYTES == 64 * 1024
    assert ArtifactContentType.JSON.value == "application/json"
    assert CONVERSATION_ARTIFACT_KEY == "conversations/{conversation_id}/{kind}/{job_id}.json"
