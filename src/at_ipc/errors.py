"""
Exception hierarchy for at-ipc.

All exceptions derive from IPCError.
"""

from typing import Any


class IPCError(Exception):
    """Base exception for all at-ipc errors."""


class IPCConnectionError(IPCError):
    """Raised when socket connection fails, target is not found, or connection times out."""


class IPCDisconnectError(IPCError):
    """Raised when peer drops or closes connection (including clean EOF)."""


class IPCTimeoutError(IPCError):
    """Raised when a command response, read assembly, or ready gate times out."""

    def __init__(self, message: str, correlation_id: str | None = None) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id


class IPCProtocolError(IPCError):
    """Raised when a message envelope is malformed, truncated, or exceeds size limits."""


class IPCSendError(IPCError):
    """Raised when a write fails because the socket is closed or broken."""


class IPCResponseError(IPCError):
    """Raised when the remote peer returns an explicit error response envelope."""

    def __init__(
        self,
        message: str,
        correlation_id: str | None = None,
        error_payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.correlation_id = correlation_id
        self.error_payload = error_payload or {}
