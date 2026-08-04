"""Authentication failures raised while resolving callers and credentials."""

from muninn.constants import ErrorKind
from muninn.exceptions import MuninnError


class AuthError(MuninnError):
    """Represent an authentication or authorization failure."""


class AuthenticationRequiredError(AuthError):
    """Represent a request that does not contain bearer authentication."""

    kind = ErrorKind.AUTHENTICATION_REQUIRED
    status_code = 401
    safe_message = "Bearer authentication required"
    response_headers = {"WWW-Authenticate": "Bearer"}


class CapabilityDeniedError(AuthError):
    """Represent an authenticated caller lacking an operation capability."""

    kind = ErrorKind.CAPABILITY_DENIED
    status_code = 403
    safe_message = "Capability denied"


class CredentialError(AuthError):
    """Report a stable authentication error code."""

    status_code = 401
    safe_message = "Credential rejected"
    response_headers = {"WWW-Authenticate": "Bearer"}

    def __init__(self, message: str, *, code: str | None = None, base_message: str | None = None) -> None:
        self.code = code or message
        super().__init__(f"{self.code}: {message}" if code else message, base_message=base_message)

    @property
    def kind(self) -> ErrorKind:
        """Return the public error kind represented by the credential code."""
        try:
            return ErrorKind(self.code)
        except ValueError:
            return ErrorKind.CREDENTIAL_INVALID
