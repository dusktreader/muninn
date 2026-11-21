"""FastAPI authentication and capability dependencies."""

from uuid import UUID

import jwt
from fastapi import Header, Query, Request

from muninn.api.exceptions import RecordingAccessGrantError
from muninn.auth.exceptions import AuthenticationRequiredError, CapabilityDeniedError
from muninn.auth.jwt import Principal, decode_bearer
from muninn.config import service_settings
from muninn.constants import Environment


def principal(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    """Resolve a local principal or a bearer credential through Keycloak OIDC."""
    if (
        service_settings.auth_mode == "local"
        and service_settings.environment == Environment.LOCAL
        and authorization is None
    ):
        return Principal(
            subject="local-development",
            capabilities=frozenset(
                {
                    "context:read",
                    "metadata:write",
                    "processing:admin",
                    "deletion:write",
                    "notes:human:create",
                    "notes:human:edit",
                    "notes:human:delete",
                }
            ),
        )
    authorization = AuthenticationRequiredError.ensure_type(authorization, str, "Bearer authentication required")
    AuthenticationRequiredError.require_condition(authorization.startswith("Bearer "), "Bearer authentication required")
    return decode_bearer(authorization.removeprefix("Bearer "), request_id=request.state.request_id)


def recording_principal(
    request: Request,
    conversation_id: UUID,
    grant: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
) -> Principal:
    """Resolve an authenticated caller or conversation-scoped browser playback grant."""
    if grant is None:
        return principal(request, authorization)
    if service_settings.artifact_grant_secret is None:
        raise RecordingAccessGrantError("Recording was not found")
    try:
        claims = jwt.decode(
            grant,
            service_settings.artifact_grant_secret,
            algorithms=["HS256"],
            audience="muninn-recording",
            options={"require": ["exp", "sub", "conversation_id", "capability", "artifact_key", "conversation_revision"]},
        )
    except Exception as exc:
        raise RecordingAccessGrantError("Recording was not found") from exc
    if claims.get("capability") != "context:read" or claims.get("conversation_id") != str(conversation_id):
        raise RecordingAccessGrantError("Recording was not found")
    return Principal(str(claims["sub"]), frozenset({"context:read"}))


def require_capability(name: str, caller: Principal) -> None:
    """Reject a principal without the capability required by an operation."""
    CapabilityDeniedError.require_condition(name in caller.capabilities, "Capability denied")
