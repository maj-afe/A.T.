"""
Async Unix socket client implementation for at-ipc.

Manages socket connection, command/response correlation tracking, event
subscriptions with per-connection FIFO dispatch, and disconnect detection.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
from pathlib import Path
from typing import Any
import uuid

from ._constants import (
    CONNECTION_TIMEOUT,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_SOCKET_DIR,
    STREAM_READER_LIMIT,
)
from .envelope import Envelope, create_envelope
from .errors import (
    IPCConnectionError,
    IPCDisconnectError,
    IPCProtocolError,
    IPCResponseError,
    IPCSendError,
    IPCTimeoutError,
)
from .protocol import ConnectionWriter, read_message

logger = logging.getLogger(__name__)


class IPCClient:
    """
    Async Unix socket client for communicating with other AT services.

    Supports command/response request correlation with per-request timeouts,
    fire-and-forget events, subscription handling, and sub-millisecond disconnect
    propagation.
    """

    def __init__(
        self,
        module_name: str,
        target_module: str,
        socket_path: Path | None = None,
    ) -> None:
        self.module_name = module_name
        self.target_module = target_module
        self.socket_path = (
            Path(socket_path)
            if socket_path is not None
            else (DEFAULT_SOCKET_DIR / f"{target_module}.sock")
        )

        self._conn_writer: ConnectionWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._pending: dict[str, asyncio.Future[Envelope]] = {}
        self._event_handlers: dict[
            str, list[Callable[[Envelope, bytes | None], Awaitable[None]]]
        ] = {}
        self._on_disconnect_cb: Callable[[], Awaitable[None]] | None = None

        self._read_task: asyncio.Task | None = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._connected: bool = False
        self._disconnecting: bool = False

    @property
    def is_connected(self) -> bool:
        """Return True if currently connected to the server."""
        return self._connected

    def on(
        self,
        name: str,
        callback: Callable[[Envelope, bytes | None], Awaitable[None]] | None = None,
    ) -> Any:
        """
        Subscribe to incoming events by name.

        Can be used as a method call or decorator:
          client.on("event_name", callback)
          @client.on("event_name")
          async def callback(env, raw): ...

        The callback receives (envelope: Envelope, raw_data: bytes | None).
        Callbacks are executed sequentially inline to preserve FIFO ordering.
        """
        if callback is None:
            def decorator(fn: Callable[[Envelope, bytes | None], Awaitable[None]]):
                self._event_handlers.setdefault(name, []).append(fn)
                return fn
            return decorator

        self._event_handlers.setdefault(name, []).append(callback)
        return None

    def on_disconnect(
        self, callback: Callable[[], Awaitable[None]] | None = None
    ) -> Any:
        """
        Register a callback invoked when the peer server drops connection.

        Can be used as a method call or decorator:
          client.on_disconnect(callback)
          @client.on_disconnect
          async def callback(): ...
        """
        if callback is None:
            def decorator(fn: Callable[[], Awaitable[None]]):
                self._on_disconnect_cb = fn
                return fn
            return decorator

        self._on_disconnect_cb = callback
        return None

    async def connect(self) -> None:
        """
        Connect to the target Unix domain socket within CONNECTION_TIMEOUT.

        Raises:
            IPCConnectionError: If socket does not exist, connection refused, or timed out.
        """
        if self._connected:
            return

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(
                    str(self.socket_path), limit=STREAM_READER_LIMIT
                ),
                timeout=CONNECTION_TIMEOUT,
            )
        except (FileNotFoundError, ConnectionRefusedError, PermissionError) as exc:
            raise IPCConnectionError(
                f"Failed to connect to {self.socket_path}: {exc}"
            ) from exc
        except asyncio.TimeoutError as exc:
            raise IPCConnectionError(
                f"Connection to {self.socket_path} timed out after {CONNECTION_TIMEOUT}s"
            ) from exc

        self._reader = reader
        self._conn_writer = ConnectionWriter(writer)
        self._connected = True
        self._disconnecting = False

        # Identify ourselves immediately so the server can attribute this
        # connection in disconnect callbacks even for read-only subscribers.
        hello_env = create_envelope(
            from_module=self.module_name,
            to_module=self.target_module,
            type="event",
            name="hello",
            payload={"module": self.module_name},
        )
        await self._conn_writer.write_message(hello_env)

        self._read_task = asyncio.create_task(self._read_loop())
        logger.debug(
            "IPCClient '%s' connected to '%s' (%s)",
            self.module_name,
            self.target_module,
            self.socket_path,
        )

    async def wait_ready(self, timeout: float = DEFAULT_COMMAND_TIMEOUT) -> None:
        """
        Block until the connected server emits the 'ready' protocol event.

        Raises:
            IPCTimeoutError: If ready signal is not received within timeout.
            IPCDisconnectError: If connection dropped before ready signal arrived.
        """
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise IPCTimeoutError(
                f"Server ready timeout after {timeout}s"
            ) from exc

        if not self._connected:
            raise IPCDisconnectError("Disconnected before ready")

    async def send_command(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
        raw_data: bytes | None = None,
    ) -> Envelope:
        """
        Send a command and await its correlated response.

        Args:
            name: Specific command message name.
            payload: JSON payload dict.
            timeout: Per-command timeout in seconds (default: 10s).
            raw_data: Optional raw binary payload.

        Returns:
            The correlated response Envelope.

        Raises:
            IPCSendError: If client is not connected or writing fails.
            IPCTimeoutError: If response is not received within timeout.
            IPCResponseError: If server returned an explicit error response.
            IPCDisconnectError: If connection is dropped while waiting.
        """
        if not self._connected or self._conn_writer is None:
            raise IPCSendError("Not connected")

        correlation_id = str(uuid.uuid4())
        future: asyncio.Future[Envelope] = (
            asyncio.get_running_loop().create_future()
        )
        envelope = create_envelope(
            from_module=self.module_name,
            to_module=self.target_module,
            type="command",
            name=name,
            payload=payload if payload is not None else {},
            correlation_id=correlation_id,
        )

        try:
            self._pending[correlation_id] = future
            await self._conn_writer.write_message(envelope, raw_data)
            response = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise IPCTimeoutError(
                f"Command '{name}' timed out after {timeout}s",
                correlation_id=correlation_id,
            ) from exc
        finally:
            self._pending.pop(correlation_id, None)

        if response.type == "error":
            raise IPCResponseError(
                response.payload.get("error", "Remote error"),
                correlation_id=correlation_id,
                error_payload=response.payload,
            )

        return response

    async def send_event(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        raw_data: bytes | None = None,
    ) -> None:
        """
        Send a fire-and-forget event message without expecting a response.

        Args:
            name: Specific event name.
            payload: JSON payload dict.
            raw_data: Optional raw binary payload.
        """
        if not self._connected or self._conn_writer is None:
            raise IPCSendError("Not connected")

        envelope = create_envelope(
            from_module=self.module_name,
            to_module=self.target_module,
            type="event",
            name=name,
            payload=payload if payload is not None else {},
        )
        await self._conn_writer.write_message(envelope, raw_data)

    async def close(self) -> None:
        """Close the socket connection and cancel any pending read tasks."""
        if not self._connected and self._conn_writer is None:
            return

        self._disconnecting = True
        self._connected = False

        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None

        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(
                    IPCDisconnectError("Client closed connection")
                )
        self._pending.clear()

        if self._conn_writer is not None:
            self._conn_writer.close()
            try:
                await self._conn_writer.wait_closed()
            except Exception:
                pass
            self._conn_writer = None

        logger.debug("IPCClient '%s' closed", self.module_name)

    async def _read_loop(self) -> None:
        """Background loop reading and dispatching inbound messages in FIFO order."""
        try:
            while True:
                if self._reader is None:
                    break
                envelope, raw_data = await read_message(self._reader)

                # Ready protocol check
                if envelope.name == "ready":
                    self._ready_event.set()
                    continue

                # Command response / error correlation
                if envelope.type in ("response", "error") and envelope.correlation_id:
                    future = self._pending.get(envelope.correlation_id)
                    if future is not None and not future.done():
                        future.set_result(envelope)
                    else:
                        logger.warning(
                            "Response received with unknown correlation_id: %s",
                            envelope.correlation_id,
                        )
                    continue

                # Inbound event dispatch: strictly sequential inline to guarantee FIFO
                handlers = self._event_handlers.get(envelope.name, [])
                for handler in handlers:
                    try:
                        await handler(envelope, raw_data)
                    except Exception as exc:
                        logger.exception(
                            "Event callback for '%s' raised exception",
                            envelope.name,
                        )

        except (
            IPCDisconnectError,
            ConnectionResetError,
            asyncio.IncompleteReadError,
            IPCTimeoutError,
        ):
            pass
        except IPCProtocolError as exc:
            logger.warning("Protocol error in client read loop: %s", exc)
        finally:
            self._connected = False
            self._ready_event.set()

            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        IPCDisconnectError("Server disconnected")
                    )
            self._pending.clear()

            if self._on_disconnect_cb is not None and not self._disconnecting:
                try:
                    await self._on_disconnect_cb()
                except Exception as exc:
                    logger.exception("Error in client disconnect callback: %s", exc)
