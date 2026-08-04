"""Validate Keycloak JWT signatures and claims without leaking token contents."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import jwt
from loguru import logger

from muninn.config import service_settings
from muninn.auth.exceptions import CredentialError

ALLOWED_ALGORITHMS = ("RS256",)
ALLOWED_CAPABILITIES = frozenset(
    {
        "context:read",
        "metadata:write",
        "deletion:write",
        "processing:admin",
        "notes:human:create",
        "notes:human:edit",
        "notes:human:delete",
        "notes:agent:create",
    }
)


@dataclass(frozen=True)
class Principal:
    """Represent a safe authenticated subject."""

    subject: str
    capabilities: frozenset[str]
    token_id: str | None = None


def validate_claims(claims: dict[str, Any]) -> Principal:
    """Validate required claims and retain only capabilities understood by this service."""
    now = datetime.now(timezone.utc).timestamp()
    CredentialError.require_condition(
        claims.get("iss") == _issuer_url(),
        "credential issuer is invalid",
        raise_kwargs={"code": "credential_wrong_issuer"},
    )
    audience = claims.get("aud", [])
    CredentialError.require_condition(
        service_settings.oidc_audience in ([audience] if isinstance(audience, str) else audience),
        "credential audience is invalid",
        raise_kwargs={"code": "credential_wrong_audience"},
    )
    expiration = claims.get("exp")
    CredentialError.require_condition(
        bool(claims.get("sub")) and isinstance(expiration, (int, float)),
        "credential claims are invalid",
        raise_kwargs={"code": "credential_invalid"},
    )
    CredentialError.require_condition(
        isinstance(expiration, (int, float)) and expiration >= now,
        "credential has expired",
        raise_kwargs={"code": "credential_expired"},
    )
    not_before = claims.get("nbf", now)
    CredentialError.require_condition(
        isinstance(not_before, (int, float)) and not_before <= now,
        "credential is not active",
        raise_kwargs={"code": "credential_not_active"},
    )
    token_id = claims.get("jti")
    CredentialError.require_condition(
        not token_id or token_id not in _revoked_token_ids,
        "credential has been revoked",
        raise_kwargs={"code": "credential_revoked"},
    )
    capabilities = claims.get("capabilities", [])
    CredentialError.require_condition(
        isinstance(capabilities, list) and all(isinstance(item, str) for item in capabilities),
        "credential capabilities are invalid",
        raise_kwargs={"code": "credential_invalid"},
    )
    return Principal(str(claims["sub"]), frozenset(set(capabilities) & ALLOWED_CAPABILITIES), token_id)


_jwks_clients: dict[str, jwt.PyJWKClient] = {}
_revoked_token_ids: set[str] = set()


def revoke_token(token_id: str) -> None:
    """Revoke one token identifier for the running service process."""
    _revoked_token_ids.add(token_id)


def _issuer_url() -> str:
    """Return the configured token issuer URL."""
    if service_settings.oidc_issuer_url is None:
        raise CredentialError("credential_invalid")
    return str(service_settings.oidc_issuer_url)


def _jwks_url() -> str:
    """Return the configured JWKS endpoint."""
    if service_settings.oidc_jwks_url is None:
        raise CredentialError("credential_invalid")
    return str(service_settings.oidc_jwks_url)


def _signing_key(token: str) -> Any:
    """Fetch the signing key for a token from the configured issuer's JWKS endpoint."""
    url = _jwks_url()
    client = _jwks_clients.setdefault(url, jwt.PyJWKClient(url))
    return client.get_signing_key_from_jwt(token).key


def _log_rejection(exception: Exception, request_id: str | None) -> None:
    logger.warning(
        "credential rejected request_id={} exception_class={} exception_message={!r}",
        request_id or "-",
        type(exception).__name__,
        str(exception),
    )


def decode_bearer(token: str, *, request_id: str | None = None) -> Principal:
    """Verify a JWT signature, issuer, audience, and required lifetime claims."""
    try:
        claims = jwt.decode(
            token,
            key=_signing_key(token),
            algorithms=list(ALLOWED_ALGORITHMS),
            issuer=_issuer_url(),
            audience=service_settings.oidc_audience,
            leeway=300,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        _log_rejection(exc, request_id)
        raise CredentialError("credential_expired") from exc
    except jwt.InvalidIssuerError as exc:
        _log_rejection(exc, request_id)
        raise CredentialError("credential_wrong_issuer") from exc
    except jwt.InvalidAudienceError as exc:
        _log_rejection(exc, request_id)
        raise CredentialError("credential_wrong_audience") from exc
    except jwt.ImmatureSignatureError as exc:
        _log_rejection(exc, request_id)
        raise CredentialError("credential_not_active") from exc
    except Exception as exc:
        _log_rejection(exc, request_id)
        raise CredentialError("credential_invalid") from exc
    return validate_claims(claims)
