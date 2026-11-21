"""Verify nullable optional service settings and storage credentials."""

from unittest.mock import Mock

import pytest
from pydantic import AnyHttpUrl

from muninn.config import ServiceSettings
from muninn.storage import ObjectStore


@pytest.mark.unit
def test_optional_settings_default_to_none_and_model_defaults_are_configured() -> None:
    """Represent credentials as unset while supplying usable configurable model defaults."""
    settings = ServiceSettings(
        oidc_issuer_url=AnyHttpUrl("https://issuer.example.com"),
        oidc_jwks_url=AnyHttpUrl("https://issuer.example.com/.well-known/jwks.json"),
    )

    assert settings.object_store_access_key is None
    assert settings.object_store_secret_key is None
    assert settings.assemblyai_api_key is None
    assert settings.assemblyai_transcription_model == "universal-3-5-pro"
    assert settings.assemblyai_summary_model == "gemma-4-31b"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("issuer_url", "jwks_url"),
    [
        (None, AnyHttpUrl("https://issuer.example.com/.well-known/jwks.json")),
        (AnyHttpUrl("https://issuer.example.com"), None),
    ],
)
def test_oidc_mode_requires_issuer_and_jwks_urls(
    issuer_url: AnyHttpUrl | None,
    jwks_url: AnyHttpUrl | None,
) -> None:
    with pytest.raises(ValueError, match="OIDC authentication requires MUNINN_OIDC_"):
        ServiceSettings(oidc_issuer_url=issuer_url, oidc_jwks_url=jwks_url)


@pytest.mark.unit
def test_explicit_summary_model_override_is_preserved() -> None:
    """Keep an explicitly configured AssemblyAI summary model unchanged."""
    settings = ServiceSettings(
        oidc_issuer_url=AnyHttpUrl("https://issuer.example.com"),
        oidc_jwks_url=AnyHttpUrl("https://issuer.example.com/.well-known/jwks.json"),
        assemblyai_summary_model="account-enabled-model",
    )

    assert settings.assemblyai_summary_model == "account-enabled-model"


@pytest.mark.unit
def test_url_settings_are_validated_and_defaults_are_url_values() -> None:
    """Validate HTTP and NATS URL settings while retaining their URL types."""
    settings = ServiceSettings(
        oidc_issuer_url=AnyHttpUrl("https://issuer.example.com"),
        oidc_jwks_url=AnyHttpUrl("https://issuer.example.com/.well-known/jwks.json"),
        assemblyai_api_base_url=AnyHttpUrl("https://assemblyai.example.com"),
    )

    assert str(settings.object_store_endpoint) == "http://minio:9000/"
    assert str(settings.nats_url) == "nats://localhost:4222"
    assert str(settings.assemblyai_api_base_url) == "https://assemblyai.example.com/"

    with pytest.raises(ValueError):
        ServiceSettings.model_validate(
            {
                "oidc_issuer_url": "https://issuer.example.com",
                "oidc_jwks_url": "https://issuer.example.com/.well-known/jwks.json",
                "object_store_endpoint": "not-a-url",
            }
        )


@pytest.mark.unit
def test_object_store_passes_unset_credentials_to_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass None credentials through so boto3 can use its normal credential chain."""
    client = Mock()
    boto_client = Mock(return_value=client)
    monkeypatch.setattr("muninn.storage.service.boto3.client", boto_client)

    ObjectStore(endpoint_url="https://minio.test", bucket="muninn").client()

    boto_client.assert_called_once_with(
        "s3",
        endpoint_url="https://minio.test",
        aws_access_key_id=None,
        aws_secret_access_key=None,
    )
