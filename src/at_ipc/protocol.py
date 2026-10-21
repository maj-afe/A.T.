"""
Wire protocol implementation for at-ipc: ConnectionWriter and read_message.

Handles message framing, serialization, raw binary payloads, write locking,
and timeouts.
"""

import asyncio
import json
import logging
import socket
from typing import Any

from ._constants import HEADER_READ_TIMEOUT, RAW_PAYLOAD_READ_TIMEOUT
from .envelope import Envelope, dict_to_envelope, envelope_to_dict
from .errors import (
    IPCDisconnectError,
    IPCProtocolError,
    IPCSendError,
    IPCTimeoutError,
)

logger = logging.getLogger(__name__)


class ConnectionWriter:
    """
    Wraps an asyncio.StreamWriter with an asyncio.Lock to ensure atomic writes.

    Provides serialized writes for control envelopes and binary payloads,
    preventing interleaved frames across concurrent tasks.
    """

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    @property
    def raw_writer(self) -> asyncio.StreamWriter:
        """Access underlying asyncio StreamWriter."""
        return self._writer

    def is_congested(self, high_water: int = 65536) -> bool:
        """
        Return True if this connection's write buffer is backing up.

        Congestion means the transport has queued at least `high_water` bytes
        that the peer has not yet consumed. A write that is merely in progress
        (lock held) is not congestion: services that both answer commands and
        broadcast must not drop frames while a short response is being written.
        """
        return self._writer.transport.get_write_buffer_size() >= high_water

    async def write_message(
        self, envelope: Envelope, raw_data: bytes | None = None
    ) -> None:
        """
        Write a message envelope and optional raw byte payload atomically.

        Args:
            envelope: Envelope dataclass to transmit.
            raw_data: Optional raw binary payload.

        Raises:
            IPCSendError: If the socket is closed, reset, or broken.
            IPCProtocolError: If envelope serialization fails or limits are exceeded.
        """
        if raw_data is not None:
            envelope.bytes_length = len(raw_data)

        header_dict = envelope_to_dict(envelope)
        header_line = (
            json.dumps(header_dict, separators=(",", ":")).encode("utf-8") + b"\n"
        )

        async with self._lock:
            try:
                self._writer.write(header_line)
                if raw_data is not None:
                    self._writer.write(raw_data)
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                raise IPCSendError(f"Write failed: {exc}") from exc

    def close(self) -> None:
        """Close the underlying writer and immediately shutdown socket."""
        try:
            sock = self._writer.get_extra_info("socket")
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            self._writer.close()
        except Exception as exc:
            logger.debug("Error closing writer: %s", exc)

    async def wait_closed(self) -> None:
        """
        Wait until writer is completely closed.

        A writer closed during broadcast eviction may still flush its buffer to
        an already-reset peer, so BrokenPipeError/ConnectionResetError are
        expected and must never propagate out of teardown paths.
        """
        try:
            await self._writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.debug("Socket error while waiting for writer to close: %s", exc)
        except Exception as exc:
            logger.debug("Error waiting for writer to close: %s", exc)


async def read_message(
    reader: asyncio.StreamReader,
) -> tuple[Envelope, bytes | None]:
    """
    Read a single complete message (JSON header + optional raw payload) from reader.

    Returns:
        tuple of (Envelope, raw_data | None)

    Raises:
        IPCDisconnectError: If peer closed connection (clean EOF or reset).
        IPCTimeoutError: If reading the header or raw payload times out.
        IPCProtocolError: If message is malformed, truncated, or exceeds line limit.
    """
    # Step 1: Read newline-terminated JSON header line
    try:
        line = await asyncio.wait_for(
            reader.readline(), timeout=HEADER_READ_TIMEOUT
        )
    except asyncio.TimeoutError as exc:
        raise IPCTimeoutError("Header read timed out") from exc
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        # On transport failure asyncio injects the transport's own exception
        # (often BrokenPipeError from a failed flush) into pending reads
        # instead of signalling clean EOF. That is a disconnect, not a crash.
        raise IPCDisconnectError(f"Connection failed while reading header: {exc}") from exc
    except ValueError as exc:
        raise IPCProtocolError(f"Line length exceeded limit: {exc}") from exc

    # Immediate EOF check before JSON parsing to prevent 100% CPU infinite loop
    if not line:
        raise IPCDisconnectError("Connection closed (EOF)")

    # Step 2: Parse and validate JSON header
    try:
        header_dict = json.loads(line.decode("utf-8"))
        envelope = dict_to_envelope(header_dict)
    except (json.JSONDecodeError, UnicodeDecodeError, IPCProtocolError) as exc:
        raise IPCProtocolError(f"Malformed message header: {exc}") from exc

    # Step 3: Read raw payload if specified
    raw_data: bytes | None = None
    if envelope.bytes_length is not None:
        if envelope.bytes_length > 0:
            try:
                raw_data = await asyncio.wait_for(
                    reader.readexactly(envelope.bytes_length),
                    timeout=RAW_PAYLOAD_READ_TIMEOUT,
                )
            except asyncio.IncompleteReadError as exc:
                raise IPCProtocolError(
                    f"Truncated: expected {envelope.bytes_length} bytes, got {len(exc.partial)}"
                ) from exc
            except asyncio.TimeoutError as exc:
                raise IPCTimeoutError(
                    f"Timed out reading {envelope.bytes_length} byte payload"
                ) from exc
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                raise IPCDisconnectError(
                    f"Connection failed while reading payload: {exc}"
                ) from exc
        else:
            raw_data = b""

    return envelope, raw_data
