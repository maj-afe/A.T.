"""
at-ipc — Shared IPC library for the AT voice assistant system.

Provides async Unix domain socket communication between AT services with:
  - Standardized JSON message envelopes and raw binary audio payload streaming
  - Request/response correlation tracking with per-request timeouts
  - Fire-and-forget event broadcast with slow-client isolation
  - Ready startup protocol and sub-millisecond disconnect detection
  - Strict per-connection FIFO message processing
"""

from .client import IPCClient
from .envelope import Envelope, create_envelope
from .errors import (
    IPCConnectionError,
    IPCDisconnectError,
    IPCError,
    IPCProtocolError,
    IPCResponseError,
    IPCSendError,
    IPCTimeoutError,
)
from .protocol import ConnectionWriter, read_message
from .server import IPCServer

__all__ = [
    "IPCServer",
    "IPCClient",
    "Envelope",
    "create_envelope",
    "read_message",
    "ConnectionWriter",
    "IPCError",
    "IPCConnectionError",
    "IPCDisconnectError",
    "IPCTimeoutError",
    "IPCProtocolError",
    "IPCSendError",
    "IPCResponseError",
]
