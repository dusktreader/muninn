"""Failures owned by the MCP adapter."""

from muninn.constants import ErrorKind
from muninn.exceptions import MuninnError


class McpError(MuninnError):
    """Represent an MCP adapter failure."""


class McpOffsetOutOfRangeError(McpError):
    """Represent an MCP note offset outside the recording duration."""

    kind = ErrorKind.OFFSET_OUT_OF_RANGE
    status_code = 422
    safe_message = "Offset out of range"


class DelegatedSessionRequiredError(McpError):
    """Represent an agent write without its delegated session identifier."""

    kind = ErrorKind.DELEGATED_SESSION_REQUIRED
    status_code = 401
    safe_message = "Delegated session required"
