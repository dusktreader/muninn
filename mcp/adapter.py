"""Bound MCP context operations to API-shaped values."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4


@dataclass
class DelegatedSession:
    """Represent a revocable, short-lived host delegation."""

    session_id: UUID
    user_sub: str
    host: str
    capabilities: frozenset[str]
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def active(self, now: datetime | None = None) -> bool:
        """Return whether the session is currently usable."""
        current = now or datetime.now(timezone.utc)
        return self.revoked_at is None and current < self.expires_at


def exchange_session(user_sub: str, host: str, requested: set[str]) -> DelegatedSession:
    """Issue only bounded capabilities requested by a supported host."""
    if host not in {"opencode", "claude"}:
        raise ValueError("invalid_request")
    allowed = {"context:read", "notes:agent:create"}
    capabilities = frozenset(requested & allowed)
    if "context:read" not in capabilities:
        capabilities = capabilities | {"context:read"}
    issued = datetime.now(timezone.utc)
    return DelegatedSession(uuid4(), user_sub, host, capabilities, issued, issued + timedelta(hours=8))


def bounded_text(value: str, limit: int = 12_000) -> tuple[str, bool]:
    """Truncate agent content at a fixed boundary and report truncation."""
    return value[:limit], len(value) > limit
