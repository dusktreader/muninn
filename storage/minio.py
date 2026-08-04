"""MinIO/S3 adapter contracts used only by service code."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactManifest:
    """Describe an immutable service-owned artifact."""

    bucket: str
    object_key: str
    version_id: str
    size: int
    sha256: str


class ArtifactStore:
    """Define the narrow object-store operations available to the domain."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket

    def operation_key(self, conversation_id: str, operation_id: str, suffix: str) -> str:
        """Build a server-generated object key without using client filenames."""
        return f"conversations/{conversation_id}/operations/{operation_id}/{suffix}"
