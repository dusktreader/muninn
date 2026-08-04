"""OIDC validation and capability contracts."""

from muninn.auth.exceptions import AuthError, AuthenticationRequiredError, CapabilityDeniedError, CredentialError

__all__ = ["AuthError", "AuthenticationRequiredError", "CapabilityDeniedError", "CredentialError"]
