"""Translate and verify objects stored in NATS JetStream Object Store."""

from dataclasses import dataclass

from buzz import enforce_defined
from nats.js.object_store import ObjectStore

from muninn.constants import ArtifactKind
from muninn.utils import calculate_digest
from muninn.queue.contracts import ResultObject
from muninn.queue.exceptions import (
    ObjectStoreIntegrityErrors,
    ObjectStoreIntegrityError,
    ObjectStoreNotFoundErrors,
    ObjectStoreNotFoundError,
    ObjectStoreOperationErrors,
    ObjectStoreOperationError,
    ObjectStoreUnavailableErrors,
    ObjectStoreUnavailableError,
)


@dataclass(slots=True)
class JetStreamObjectStore:
    """Adapt a NATS JetStream Object Store bucket to the scoped protocol."""

    bucket: ObjectStore
    bucket_name: str = "muninn"

    async def put(self, key: str, content: bytes, kind: ArtifactKind) -> ResultObject:
        """Put bytes into the configured JetStream Object Store bucket."""
        with (
            ObjectStoreUnavailableError.handle_errors(
                "object store is unavailable",
                handle_exc_class=ObjectStoreUnavailableErrors,
            ),
            ObjectStoreOperationError.handle_errors(
                "object store rejected the object operation",
                handle_exc_class=ObjectStoreOperationErrors,
            ),
        ):
            await self.bucket.put(key, content)
        return ResultObject(
            kind=kind,
            bucket=self.bucket_name,
            key=key,
            sha256=calculate_digest(content),
            byte_size=len(content),
        )

    async def delete(self, reference: ResultObject) -> None:
        """Delete one unpublished input object from JetStream Object Store."""
        await self.delete_key(reference.key)

    async def delete_key(self, key: str) -> None:
        """Delete one object by its known key, including after an ambiguous put failure."""
        with (
            ObjectStoreUnavailableError.handle_errors(
                "object store is unavailable",
                handle_exc_class=ObjectStoreUnavailableErrors,
            ),
            ObjectStoreOperationError.handle_errors(
                "object store rejected the object operation",
                handle_exc_class=ObjectStoreOperationErrors,
            ),
        ):
            try:
                await self.bucket.delete(key)
            except ObjectStoreNotFoundErrors:
                return

    async def get(self, reference: ResultObject) -> bytes:
        """Read and verify bytes from JetStream Object Store."""
        with (
            ObjectStoreUnavailableError.handle_errors(
                "object store is unavailable",
                handle_exc_class=ObjectStoreUnavailableErrors,
            ),
            ObjectStoreNotFoundError.handle_errors(
                "object was not found",
                handle_exc_class=ObjectStoreNotFoundErrors,
            ),
            ObjectStoreIntegrityError.handle_errors(
                "object integrity verification failed",
                handle_exc_class=ObjectStoreIntegrityErrors,
            ),
            ObjectStoreOperationError.handle_errors(
                "object store rejected the object operation",
                handle_exc_class=ObjectStoreOperationErrors,
            ),
        ):
            result = await self.bucket.get(reference.key)
        content = enforce_defined(
            result.data,
            "object store returned no object data",
            raise_exc_class=ObjectStoreIntegrityError,
        )
        ObjectStoreIntegrityError.require_condition(
            len(content) == reference.byte_size and calculate_digest(content) == reference.sha256,
            "object integrity verification failed",
        )
        return content
