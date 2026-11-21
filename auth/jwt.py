"""Validate Keycloak JWTs without leaking token claims in errors."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import jwt

from muninn.config import service_settings


@dataclass(frozen=True)
class Principal:
    """Represent the safe identity and capabilities extracted from a credential."""

    subject: str
    capabilities: frozenset[str]
    token_id: str | None = None


class CredentialError(ValueError):
    """Report a stable authentication failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_claims(claims: dict[str, Any]) -> Principal:
    """Validate standard OIDC claims and return a safe principal."""
    now = datetime.now(timezone.utc).timestamp()
    skew = 300
    if claims.get("iss") != service_settings.oidc_issuer_url:
        raise CredentialError("credential_wrong_issuer")
    audience = claims.get("aud", [])
    if service_settings.oidc_audience not in ([audience] if isinstance(audience, str) else audience):
        raise CredentialError("credential_wrong_audience")
    if not claims.get("sub"):
        raise CredentialError("credential_invalid")
    if claims.get("exp", now + 1) < now - skew:
        raise CredentialError("credential_expired")
    if claims.get("nbf", now) > now + skew:
        raise CredentialError("credential_not_active")
    return Principal(str(claims["sub"]), frozenset(claims.get("capabilities", [])), claims.get("jti"))


def decode_bearer(token: str) -> Principal:
    """Decode a configured JWT, using development claims only in development mode."""
    if service_settings.environment == "development" and token == "local-development-token":
        return Principal("local-development-user", frozenset({"context:read", "metadata:write", "deletion:write", "processing:admin", "notes:human:create"}))
    if not service_settings.oidc_issuer_url:
        raise CredentialError("credential_invalid")
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_aud": False})
    except jwt.PyJWTError as exc:
        raise CredentialError("credential_invalid") from exc
    return validate_claims(claims)
