"""Test the implemented authentication claim contract."""

from datetime import datetime, timezone
from io import StringIO

import jwt
import pytest
from fastapi import Request
from loguru import logger
from pydantic import ValidationError

from muninn.auth.jwt import CredentialError, decode_bearer, validate_claims
from muninn.auth.dependencies import principal
from muninn.config import ServiceSettings, service_settings
from muninn.constants import Environment


def valid_claims() -> dict[str, object]:
    """Build valid claims against the configured issuer and audience."""
    return {
        "iss": str(service_settings.oidc_issuer_url),
        "aud": service_settings.oidc_audience,
        "sub": "owner-1",
        "exp": datetime.now(timezone.utc).timestamp() + 600,
        "capabilities": ["context:read"],
    }


def request_with_id() -> Request:
    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    request.state.request_id = "request-123"
    return request


@pytest.mark.unit
def test_valid_claims_create_a_bounded_principal() -> None:
    principal = validate_claims(valid_claims())
    assert principal.subject == "owner-1"
    assert principal.capabilities == frozenset({"context:read"})


@pytest.mark.unit
def test_local_auth_resolves_a_fixed_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_settings, "auth_mode", "local")
    monkeypatch.setattr(service_settings, "environment", Environment.LOCAL)

    caller = principal(request_with_id(), None)

    assert caller.subject == "local-development"
    assert caller.capabilities == frozenset(
        {
            "context:read",
            "metadata:write",
            "processing:admin",
            "deletion:write",
            "notes:human:create",
            "notes:human:edit",
            "notes:human:delete",
        }
    )


@pytest.mark.unit
def test_local_auth_cannot_be_configured_outside_local_environment() -> None:
    with pytest.raises(ValidationError, match="Local authentication is only available"):
        ServiceSettings(
            environment=Environment.DEV,
            auth_mode="local",
            oidc_issuer_url="https://keycloak.test/realms/muninn",
            oidc_jwks_url="https://keycloak.test/realms/muninn/protocol/openid-connect/certs",
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"iss": "https://wrong.example"}, "credential_wrong_issuer"),
        ({"aud": "wrong-audience"}, "credential_wrong_audience"),
        ({"sub": ""}, "credential_invalid"),
        ({"exp": 0}, "credential_expired"),
        ({"nbf": datetime.now(timezone.utc).timestamp() + 301}, "credential_not_active"),
    ],
)
def test_invalid_claims_return_stable_codes(change: dict[str, object], code: str) -> None:
    claims = valid_claims() | change
    with pytest.raises(CredentialError, match=code) as error:
        validate_claims(claims)
    assert error.value.code == code


@pytest.mark.unit
def test_forged_token_is_rejected_after_jwks_key_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a token whose signature verifier reports a forged signature."""
    monkeypatch.setattr(service_settings, "oidc_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr("muninn.auth.jwt._jwks_clients", {})
    monkeypatch.setattr("muninn.auth.jwt.jwt.PyJWKClient", lambda _: FakeJwksClient())
    monkeypatch.setattr(
        "muninn.auth.jwt.jwt.decode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(jwt.InvalidSignatureError("Signature verification failed")),
    )
    token = "forged.token.secret"
    stream = StringIO()
    sink_id = logger.add(stream, format="{message}", level="WARNING")
    try:
        with pytest.raises(CredentialError, match="credential_invalid"):
            decode_bearer(token, request_id="request-123")
    finally:
        logger.remove(sink_id)

    log_line = stream.getvalue().strip()
    assert "request_id=request-123" in log_line
    assert "exception_class=InvalidSignatureError" in log_line
    assert "exception_message='Signature verification failed'" in log_line
    assert token not in log_line
    assert len(log_line.splitlines()) == 1


@pytest.mark.unit
def test_wrong_audience_token_is_logged_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_settings, "oidc_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr("muninn.auth.jwt._jwks_clients", {})
    monkeypatch.setattr("muninn.auth.jwt.jwt.PyJWKClient", lambda _: FakeJwksClient())
    monkeypatch.setattr(
        "muninn.auth.jwt.jwt.decode",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(jwt.InvalidAudienceError("Audience doesn't match")),
    )
    token = "wrong-audience.token.secret"
    stream = StringIO()
    sink_id = logger.add(stream, format="{message}", level="WARNING")
    try:
        with pytest.raises(CredentialError, match="credential_wrong_audience"):
            decode_bearer(token, request_id="request-456")
    finally:
        logger.remove(sink_id)

    log_line = stream.getvalue().strip()
    assert "request_id=request-456" in log_line
    assert "exception_class=InvalidAudienceError" in log_line
    assert "exception_message=\"Audience doesn't match\"" in log_line
    assert token not in log_line
    assert len(log_line.splitlines()) == 1


@pytest.mark.unit
def test_expired_token_is_rejected_by_signature_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Map an expired signed token to the stable expiry error."""
    monkeypatch.setattr(service_settings, "oidc_jwks_url", "https://issuer.test/jwks")
    monkeypatch.setattr("muninn.auth.jwt._jwks_clients", {})
    monkeypatch.setattr("muninn.auth.jwt.jwt.PyJWKClient", lambda _: FakeJwksClient())
    monkeypatch.setattr("muninn.auth.jwt.jwt.decode", lambda *_args, **_kwargs: (_ for _ in ()).throw(jwt.ExpiredSignatureError()))
    with pytest.raises(CredentialError, match="credential_expired"):
        decode_bearer("expired.token")


@pytest.mark.unit
def test_api_bearer_dependency_calls_signature_verified_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the production API dependency on the JWT verifier rather than claim parsing alone."""
    expected = type("Principal", (), {"subject": "owner-1", "capabilities": frozenset({"context:read"})})()
    monkeypatch.setattr(
        "muninn.auth.dependencies.decode_bearer",
        lambda token, **_kwargs: expected if token == "signed-token" else None,
    )
    assert principal(request_with_id(), "Bearer signed-token") is expected


class FakeJwksClient:
    """Supply a deterministic mocked JWKS signing key to decoder tests."""

    def get_signing_key_from_jwt(self, _: str) -> object:
        """Return a placeholder key consumed by the mocked decoder."""
        return type("SigningKey", (), {"key": "mocked-jwks-key"})()
