"""Provide an in-memory Object Store fake for tests."""

from hashlib import sha256

from muninn.constants import ArtifactKind
from muninn.queue.contracts import ResultObject


class InMemoryObjectStore:
    """Provide a deterministic Object Store fake for unit and integration tests."""

    def __init__(self, bucket: str = "muninn") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes, kind: ArtifactKind) -> ResultObject:
        """Store a copy of content and return its digest and size."""
        self.objects[key] = bytes(content)
        return ResultObject(
            kind=kind,
            bucket=self.bucket,
            key=key,
            sha256=sha256(content).hexdigest(),
            byte_size=len(content),
        )

    async def get(self, reference: ResultObject) -> bytes:
        """Return referenced bytes, rejecting missing or changed objects."""
        content = self.objects[reference.key]
        if len(content) != reference.byte_size or sha256(content).hexdigest() != reference.sha256:
            raise ValueError("object reference verification failed")
        return content
