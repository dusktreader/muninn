"""Verify canonical conversation artifact storage."""

from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from muninn.constants import ArtifactKind, ProcessingErrorCode, ProcessingKind, ProcessingState
from muninn.config import service_settings
from muninn.storage import CONVERSATION_ARTIFACT_KEY, get_object_store
from muninn.queue.contracts import CompletionEvent
from muninn.queue.exceptions import ObjectStoreNotFoundError, ObjectStoreUnavailableError
from muninn.storage import ObjectStore


def _event(kind: ProcessingKind) -> CompletionEvent:
    return CompletionEvent(
        job_id=uuid4(),
        conversation_id=uuid4(),
        kind=kind,
        source_version=1,
        transcript_version=None,
        state=ProcessingState.FAILED,
        error_code=ProcessingErrorCode.UNKNOWN,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("kind", "expected_kind"),
    [
        (ProcessingKind.TRANSCRIPTION, ArtifactKind.TRANSCRIPT),
        (ProcessingKind.SUMMARIZATION, ArtifactKind.SUMMARY),
    ],
)
def test_put_conversation_artifact_selects_type_key_and_digest(
    monkeypatch: pytest.MonkeyPatch,
    kind: ProcessingKind,
    expected_kind: ArtifactKind,
) -> None:
    event = _event(kind)
    content = b'{"text":"processed"}'
    calls: list[tuple[str, bytes, ArtifactKind]] = []

    def put_bytes(_: ObjectStore, key: str, value: bytes, kind: ArtifactKind) -> ArtifactKind:
        calls.append((key, value, kind))
        return kind

    monkeypatch.setattr(ObjectStore, "put_bytes", put_bytes)

    result = ObjectStore(endpoint_url="https://minio.test", bucket="muninn").put_conversation_artifact(event, content)

    expected_key = CONVERSATION_ARTIFACT_KEY.format(
        conversation_id=event.conversation_id,
        kind=expected_kind.value,
        job_id=event.job_id,
    )
    assert calls == [(expected_key, content, expected_kind)]
    assert result.kind is expected_kind
    assert result.key == expected_key
    assert result.sha256 == sha256(content).hexdigest()


@pytest.mark.unit
@pytest.mark.parametrize("error_code", ["404", "NoSuchKey", "NoSuchObject"])
def test_get_bytes_maps_missing_objects_to_not_found(
    monkeypatch: pytest.MonkeyPatch, error_code: str
) -> None:
    """Represent each S3 missing-object response as an object-store not-found error."""
    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")

    def get_object(**_: str) -> None:
        raise ClientError({"Error": {"Code": error_code}}, "GetObject")

    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(get_object=get_object))

    with pytest.raises(ObjectStoreNotFoundError):
        store.get_bytes("missing.json")


@pytest.mark.unit
def test_get_bytes_does_not_treat_other_client_errors_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve non-missing storage failures as project errors for the API handler."""
    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")

    def get_object(**_: str) -> None:
        raise ClientError({"Error": {"Code": "InternalError"}}, "GetObject")

    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(get_object=get_object))

    with pytest.raises(ObjectStoreUnavailableError):
        store.get_bytes("broken.json")


@pytest.mark.unit
def test_put_stream_persists_user_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    calls: list[dict[str, object]] = []

    def put_object(**request: object) -> None:
        calls.append(request)

    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(put_object=put_object))
    metadata = {"sha256": "a" * 64}

    store.put_stream("playback.webm", BytesIO(b"webm"), 4, ArtifactKind.RECORDING, "audio/webm", metadata)

    assert calls[0]["Metadata"] == metadata


@pytest.mark.unit
def test_get_object_stream_sends_exact_range_to_s3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the requested inclusive byte range to the S3 object store."""
    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    calls: list[dict[str, object]] = []
    result = {"Body": object()}

    def get_object(**request: object) -> dict[str, object]:
        calls.append(request)
        return result

    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(get_object=get_object))

    assert store.get_object_stream("recording", (1234, 4095)) == result
    assert calls == [{"Bucket": "muninn", "Key": "recording", "Range": "bytes=1234-4095"}]


@pytest.mark.unit
def test_get_bytes_closes_botocore_streaming_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close botocore's stream without relying on urllib3-only release methods."""
    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    payload = b"recording bytes"
    raw_stream = BytesIO(payload)
    body = StreamingBody(raw_stream, len(payload))
    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(get_object=lambda **_: {"Body": body}))

    assert store.get_bytes("recording.wav") == payload
    assert raw_stream.closed


@pytest.mark.unit
def test_get_object_store_is_configured_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure one API dependency from service settings and reuse it."""
    monkeypatch.setattr(service_settings, "object_store_endpoint", "https://configured-minio.test")
    monkeypatch.setattr(service_settings, "object_store_bucket", "configured-bucket")
    monkeypatch.setattr(service_settings, "object_store_access_key", "access")
    monkeypatch.setattr(service_settings, "object_store_secret_key", "secret")
    get_object_store.cache_clear()
    try:
        first = get_object_store()
        second = get_object_store()

        assert first is second
        assert first.endpoint_url == "https://configured-minio.test"
        assert first.bucket == "configured-bucket"
        assert first.access_key == "access"
        assert first.secret_key == "secret"
    finally:
        get_object_store.cache_clear()


@pytest.mark.unit
def test_object_store_reuses_boto_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Create the boto client once while allowing instance-level test doubles."""
    clients: list[object] = []

    def create_client(*_: object, **__: object) -> object:
        client = object()
        clients.append(client)
        return client

    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    monkeypatch.setattr("muninn.storage.service.boto3.client", create_client)

    assert store.client() is store.client()
    assert len(clients) == 1


@pytest.mark.unit
@pytest.mark.parametrize("error_code", ["404", "NoSuchBucket", "NotFound"])
def test_ensure_bucket_creates_a_missing_bucket(monkeypatch: pytest.MonkeyPatch, error_code: str) -> None:
    """Create the configured bucket only when the object store reports it missing."""
    created: list[str] = []

    def head_bucket(**_: str) -> None:
        raise ClientError({"Error": {"Code": error_code}}, "HeadBucket")

    def create_bucket(**kwargs: str) -> None:
        created.append(kwargs["Bucket"])

    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    monkeypatch.setattr(store, "client", lambda: SimpleNamespace(head_bucket=head_bucket, create_bucket=create_bucket))

    store.ensure_bucket()

    assert created == ["muninn"]


@pytest.mark.unit
def test_ensure_bucket_does_not_recreate_an_existing_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an existing object-store bucket intact."""
    def unexpected_create(**_: str) -> None:
        raise AssertionError("unexpected create")

    store = ObjectStore(endpoint_url="https://minio.test", bucket="muninn")
    client = SimpleNamespace(head_bucket=lambda **_: None, create_bucket=unexpected_create)
    monkeypatch.setattr(store, "client", lambda: client)

    store.ensure_bucket()
