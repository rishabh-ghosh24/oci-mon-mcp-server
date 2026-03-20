"""Request-scoped identity context for authenticated MCP calls."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequestIdentity:
    """Resolved identity for the current request."""

    profile_id: str
    user_id: str
    token: str | None = None
    client_type: str | None = None


_current_identity: ContextVar[RequestIdentity | None] = ContextVar(
    "oci_mon_current_identity",
    default=None,
)


def get_current_identity() -> RequestIdentity | None:
    """Return the active request identity, if one exists."""
    return _current_identity.get()



def set_current_identity(identity: RequestIdentity | None) -> Token[RequestIdentity | None]:
    """Store request identity in the current execution context."""
    return _current_identity.set(identity)



def reset_current_identity(token: Token[RequestIdentity | None]) -> None:
    """Restore the previous identity context."""
    _current_identity.reset(token)
