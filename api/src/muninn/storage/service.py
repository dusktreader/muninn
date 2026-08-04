"""Implement MinIO-backed object storage used by the API and job manager."""

from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from typing import Any, BinaryIO

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from muninn.constants import ArtifactKind, ProcessingKind
from muninn.config import service_settings
from muninn.utils import calculate_digest
from muninn.queue.contracts import CompletionEvent
from muninn.queue.exceptions import ObjectStoreNotFoundError, ObjectStoreUnavailableError
from muninn.storage.constants import ArtifactContentType, CONVERSATION_ARTIFACT_KEY, MISSING_OBJECT_ERROR_CODES
from muninn.storage.schemas import StoredConversationArtifact


@dataclass
class ObjectStore:
    """Upload, read, and delete private objects in MinIO."""

    endpoint_url: str
    bucket: str
    access_key: str | None = None
    secret_key: str | None = None
    _client: Any = field(default=None, init=False, repr=False)

    def client(self) -> Any:
        """Return the cached S3 client for the control-plane process."""
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        return self._client

    def ensure_bucket(self) -> None:
        """Create the configured bucket when the object store does not have it."""
        client = self.client()
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            client.create_bucket(Bucket=self.bucket)

    def get_object_metadata(self, key: str) -> dict[str, Any]:
        """Return the stored object size and media type without reading its content."""
        try:
            return self.client().head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in MISSING_OBJECT_ERROR_CODES | {"NoSuchBucket"}:
                raise ObjectStoreNotFoundError(f"Object was not found: {key}") from exc
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc
        except BotoCoreError as exc:
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc

    def get_object_stream(self, key: str, byte_range: tuple[int, int] | None = None) -> dict[str, Any]:
        """Open an object or a bounded byte range for streamed delivery."""
        request: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if byte_range is not None:
            request["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        try:
            return self.client().get_object(**request)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in MISSING_OBJECT_ERROR_CODES | {"NoSuchBucket"}:
                raise ObjectStoreNotFoundError(f"Object was not found: {key}") from exc
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc
        except BotoCoreError as exc:
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc

    def put_stream(
        self,
        key: str,
        content: BinaryIO,
        content_length: int,
        kind: ArtifactKind,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ArtifactKind:
        """Store one immutable artifact from a bounded stream and return its type."""
        stored_content_type = content_type or (
            ArtifactContentType.RECORDING.value
            if kind is ArtifactKind.RECORDING
            else ArtifactContentType.JSON.value
        )
        self.client().put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=stored_content_type,
            ContentLength=content_length,
            **({"Metadata": metadata} if metadata is not None else {}),
        )
        return kind

    def delete_keys(self, keys: list[str]) -> None:
        """Delete known conversation objects without requiring a prefix listing."""
        if keys:
            self.client().delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": key} for key in keys]})

    def delete_conversation(self, conversation_id: str) -> None:
        """Best-effort delete every object under one conversation prefix."""
        client = self.client()
        pages = client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=f"conversations/{conversation_id}/"
        )
        for page in pages:
            keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

    def get_bytes(self, key: str, limit: int = 512 * 1024 * 1024) -> bytes:
        """Read a bounded source or artifact object for a manager operation."""
        try:
            response = self.client().get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
            try:
                return body.read(limit)
            finally:
                body.close()
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in MISSING_OBJECT_ERROR_CODES:
                raise ObjectStoreNotFoundError(f"Object was not found: {key}") from exc
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc
        except BotoCoreError as exc:
            raise ObjectStoreUnavailableError(f"Object Store read failed for {key}") from exc

    def put_bytes(self, key: str, content: bytes, kind: ArtifactKind) -> ArtifactKind:
        """Store bounded artifact bytes in MinIO and return their type."""
        return self.put_stream(key, BytesIO(content), len(content), kind)

    def put_conversation_artifact(self, event: CompletionEvent, content: bytes) -> StoredConversationArtifact:
        """
        Store a processed conversation artifact under its canonical key.

        Return the artifact metadata only after the underlying storage operation succeeds.
        """
        kind = ArtifactKind.TRANSCRIPT if event.kind is ProcessingKind.TRANSCRIPTION else ArtifactKind.SUMMARY
        key = CONVERSATION_ARTIFACT_KEY.format(
            conversation_id=event.conversation_id,
            kind=kind.value,
            job_id=event.job_id,
        )
        self.put_bytes(key, content, kind)
        return StoredConversationArtifact(kind=kind, key=key, sha256=calculate_digest(content))


@lru_cache(maxsize=1)
def get_object_store() -> ObjectStore:
    """Return the process-wide configured object store for API dependencies."""
    return ObjectStore(
        endpoint_url=str(service_settings.object_store_endpoint),
        bucket=service_settings.object_store_bucket,
        access_key=service_settings.object_store_access_key,
        secret_key=service_settings.object_store_secret_key,
    )
