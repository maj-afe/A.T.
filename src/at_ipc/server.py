"""
Async Unix socket server implementation for at-ipc.

Manages socket lifecycle, connection handling, inline FIFO message dispatch,
ready protocol emission, event broadcasting, and clean shutdown.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import os
from pathlib import Path
import signal
import socket
from typing import Any

from ._constants import (
    BROADCAST_TARGET,
    BROADCAST_WRITE_TIMEOUT,
    DEFAULT_SOCKET_DIR,
    SOCKET_PERMISSIONS,
    STREAM_READER_LIMIT,
)
from .envelope import Envelope, create_envelope
from .errors import (
    IPCDisconnectError,
    IPCError,
    IPCProtocolError,
    IPCSendError,
    IPCTimeoutError,
)
from .protocol import ConnectionWriter, read_message

logger = logging.getLogger(__name__)


class IPCServer:
    """
    Unix domain socket server for AT services.

    Listens on /run/at/{module_name}.sock (or custom path), dispatches incoming
    messages with per-connection FIFO guarantees, emits the ready protocol,
    and supports event broadcasting with slow-client isolation.
    """

    def __init__(
        self,
        module_name: str,
        socket_path: Path | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.module_name = module_name
        self.socket_path = (
            Path(socket_path)
            if socket_path is not None
            else (DEFAULT_SOCKET_DIR / f"{module_name}.sock")
        )
        self.install_signal_handlers = install_signal_handlers

        self._handlers: dict[
            str,
            Callable[[Envelope, bytes | None], Awaitable[Any]],
        ] = {}
        self._clients: dict[int, ConnectionWriter] = {}
        self._client_peers: dict[int, str] = {}
        self._client_tasks: dict[int, asyncio.Task] = {}
        self._on_disconnect_cb: Callable[[str | None], Awaitable[None]] | None = None

        self._server: asyncio.Server | None = None
        self._ready: bool = False
        self._running: bool = False
        self._signal_installed: bool = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def is_running(self) -> bool:
        """Return True if the server is currently listening."""
        return self._running

    @property
    def is_ready(self) -> bool:
        """Return True if the server has initialized and emitted ready."""
        return self._ready

    def on(
        self,
        name: str,
        handler: Callable[[Envelope, bytes | None], Awaitable[Any]] | None = None,
    ) -> Any:
        """
        Register an async callback handler for a message name.

        Can be used as a method call or decorator:
          server.on("ping", handle_ping)
          @server.on("ping")
          async def handle_ping(env, raw): ...

        The handler receives (envelope: Envelope, raw_data: bytes | None).
        For commands, the return value is automatically sent back as a response:
          - dict -> payload=dict
          - (dict, bytes) -> payload=dict, raw_data=bytes
          - None -> payload={}
        """
        if handler is None:
            def decorator(fn: Callable[[Envelope, bytes | None], Awaitable[Any]]):
                self._handlers[name] = fn
                return fn
            return decorator

        self._handlers[name] = handler
        return None

    def on_disconnect(
        self, callback: Callable[[str | None], Awaitable[None]] | None = None
    ) -> Any:
        """
        Register a callback invoked when a client disconnects.

        Can be used as a method call or decorator:
          server.on_disconnect(handle_disconnect)
          @server.on_disconnect
          async def handle_disconnect(peer): ...
        """
        if callback is None:
            def decorator(fn: Callable[[str | None], Awaitable[None]]):
                self._on_disconnect_cb = fn
                return fn
            return decorator

        self._on_disconnect_cb = callback
        return None

    async def start(self) -> None:
        """
        Bind the Unix socket, start serving connections, and emit ready state.

        Raises:
            IPCError: If socket file is already active and held by another process.
        """
        if self._running:
            return

        self._loop = asyncio.get_running_loop()

        # Check for existing socket file
        if self.socket_path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(self.socket_path)
                )
                writer.close()
                await writer.wait_closed()
                raise IPCError(f"Socket already in use: {self.socket_path}")
            except (ConnectionRefusedError, FileNotFoundError):
                # Stale socket from ungraceful shutdown -> safe to unlink
                self.socket_path.unlink(missing_ok=True)

        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)

        self._ready = True
        self._running = True

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
            limit=STREAM_READER_LIMIT,
        )

        try:
            os.chmod(self.socket_path, SOCKET_PERMISSIONS)
        except OSError as exc:
            logger.warning("Could not set 0600 permissions on %s: %s", self.socket_path, exc)

        if self.install_signal_handlers:
            try:
                self._loop.add_signal_handler(
                    signal.SIGTERM,
                    lambda: asyncio.create_task(self.stop()),
                )
                self._signal_installed = True
            except (NotImplementedError, RuntimeError) as exc:
                logger.debug("Signal handler registration skipped: %s", exc)

        logger.info("IPCServer '%s' listening on %s", self.module_name, self.socket_path)

    async def stop(self) -> None:
        """
        Gracefully stop the server, disconnect all clients, and unlink the socket.
        """
        if not self._running and self._server is None:
            return

        self._running = False
        self._ready = False

        if self._signal_installed and self._loop:
            try:
                self._loop.remove_signal_handler(signal.SIGTERM)
            except Exception:
                pass
            self._signal_installed = False

        # 1. Stop accepting new connections
        srv = self._server
        self._server = None
        if srv is not None:
            srv.close()

        # 2. Close and shutdown all client connections immediately
        writers = list(self._clients.values())
        for cw in writers:
            cw.close()

        # 3. Cancel any active client loop tasks so wait_closed() unblocks
        tasks = [t for t in self._client_tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._client_tasks.clear()
        self._clients.clear()
        self._client_peers.clear()

        # 4. Await server socket closure
        if srv is not None:
            try:
                await srv.wait_closed()
            except Exception:
                pass

        # 5. Remove socket file
        try:
            self.socket_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("Error unlinking socket file %s: %s", self.socket_path, exc)

        logger.info("IPCServer '%s' stopped cleanly", self.module_name)

    async def broadcast(
        self,
        name: str,
        payload: dict[str, Any] | None = None,
        raw_data: bytes | None = None,
        *,
        drop_on_backpressure: bool = False,
    ) -> None:
        """
        Broadcast an event to all connected clients concurrently.

        Args:
            name: Specific event name.
            payload: JSON payload dict.
            raw_data: Optional raw binary payload.
            drop_on_backpressure: If True, slow clients with full buffers drop the
                frame without closing the connection (used for real-time audio).
                If False, slow or failed clients are evicted and closed.
        """
        envelope = create_envelope(
            from_module=self.module_name,
            to_module=BROADCAST_TARGET,
            type="event",
            name=name,
            payload=payload if payload is not None else {},
        )

        async def _write_to_client(
            cid: int, cw: ConnectionWriter
        ) -> tuple[int, Exception | None]:
            # Drop BEFORE buffering: once write_message() runs, the frame is
            # already queued on the transport, so a later drain() timeout only
            # cancels the wait — it cannot un-send the frame.
            if drop_on_backpressure and cw.is_congested():
                logger.debug(
                    "Dropped broadcast '%s' to client %s (congested)", name, cid
                )
                return cid, None
            try:
                await asyncio.wait_for(
                    cw.write_message(envelope, raw_data),
                    timeout=BROADCAST_WRITE_TIMEOUT,
                )
                return cid, None
            except asyncio.TimeoutError as exc:
                if drop_on_backpressure:
                    logger.debug(
                        "Dropped broadcast '%s' to client %s (backpressure)", name, cid
                    )
                    return cid, None
                return cid, exc
            except (IPCSendError, OSError) as exc:
                return cid, exc

        clients_snapshot = list(self._clients.items())
        if not clients_snapshot:
            return

        results = await asyncio.gather(
            *[_write_to_client(cid, cw) for cid, cw in clients_snapshot],
            return_exceptions=False,
        )

        for cid, exc in results:
            if exc is not None:
                cw = self._clients.pop(cid, None)
                logger.warning(
                    "Evicting client %s during broadcast failure: %s",
                    cid,
                    exc,
                )
                if cw is not None:
                    cw.close()
                # NOTE: no disconnect callback here. _handle_client's finally
                # block is the single source of truth for disconnect events,
                # otherwise callbacks fire twice for one eviction.

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Manage client connection lifecycle and FIFO message loop."""
        conn_writer = ConnectionWriter(writer)
        cid = id(writer)
        self._clients[cid] = conn_writer

        curr_task = asyncio.current_task()
        if curr_task is not None:
            self._client_tasks[cid] = curr_task

        peer_name: str | None = None

        try:
            # Send ready event unconditionally to newly connected client
            ready_env = create_envelope(
                from_module=self.module_name,
                to_module=BROADCAST_TARGET,
                type="event",
                name="ready",
            )
            try:
                await conn_writer.write_message(ready_env)
            except IPCSendError as exc:
                logger.debug("Client disconnected before ready: %s", exc)
                return

            # Per-connection FIFO read loop: messages dispatched sequentially inline
            while self._running:
                try:
                    envelope, raw_data = await read_message(reader)
                except IPCDisconnectError:
                    break
                except IPCTimeoutError:
                    logger.debug(
                        "Client read timeout from %s, disconnecting",
                        peer_name or "unknown",
                    )
                    break
                except IPCProtocolError as exc:
                    logger.warning(
                        "Malformed message from %s: %s",
                        peer_name or "unknown",
                        exc,
                    )
                    continue

                # Protocol-level hello: lets read-only subscribers (e.g.
                # audio-source's consumers) be identified before they send
                # their first application message.
                if envelope.name == "hello" and envelope.type == "event":
                    peer_module = envelope.payload.get("module")
                    if not isinstance(peer_module, str) or not peer_module:
                        peer_module = envelope.from_module
                    peer_name = peer_module
                    self._client_peers[cid] = peer_module
                    continue

                peer_name = envelope.from_module
                self._client_peers[cid] = peer_name

                # Strict per-connection FIFO: await inline directly, no create_task
                await self._dispatch(envelope, raw_data, conn_writer)

        finally:
            self._clients.pop(cid, None)
            self._client_peers.pop(cid, None)
            self._client_tasks.pop(cid, None)

            conn_writer.close()
            try:
                await asyncio.shield(conn_writer.wait_closed())
            except Exception:
                pass

            if self._on_disconnect_cb:
                try:
                    await self._on_disconnect_cb(peer_name)
                except Exception as exc:
                    logger.exception("Error in disconnect callback: %s", exc)

    async def _dispatch(
        self,
        envelope: Envelope,
        raw_data: bytes | None,
        conn_writer: ConnectionWriter,
    ) -> None:
        """Dispatch incoming envelope to registered handler and auto-respond."""
        handler = self._handlers.get(envelope.name)

        if envelope.type == "command":
            if handler is None:
                logger.warning(
                    "No handler registered for command '%s' from %s",
                    envelope.name,
                    envelope.from_module,
                )
                err_env = create_envelope(
                    from_module=self.module_name,
                    to_module=envelope.from_module,
                    type="error",
                    name=envelope.name,
                    payload={"error": f"No handler registered for command '{envelope.name}'"},
                    correlation_id=envelope.correlation_id,
                )
                try:
                    await conn_writer.write_message(err_env)
                except IPCSendError as exc:
                    logger.debug("Failed to send error response: %s", exc)
                return

            try:
                res = await handler(envelope, raw_data)
            except Exception as exc:
                logger.exception("Handler for command '%s' raised exception", envelope.name)
                err_env = create_envelope(
                    from_module=self.module_name,
                    to_module=envelope.from_module,
                    type="error",
                    name=envelope.name,
                    payload={"error": str(exc), "type": type(exc).__name__},
                    correlation_id=envelope.correlation_id,
                )
                try:
                    await conn_writer.write_message(err_env)
                except IPCSendError as send_exc:
                    logger.debug("Failed to send error response: %s", send_exc)
                return

            # Format response payload and raw bytes
            resp_payload: dict[str, Any] = {}
            resp_raw: bytes | None = None

            if (
                isinstance(res, tuple)
                and len(res) == 2
                and isinstance(res[0], dict)
                and isinstance(res[1], (bytes, bytearray))
            ):
                resp_payload = res[0]
                resp_raw = bytes(res[1])
            elif isinstance(res, dict):
                resp_payload = res
            elif res is None:
                resp_payload = {}
            else:
                resp_payload = {"result": res}

            resp_env = create_envelope(
                from_module=self.module_name,
                to_module=envelope.from_module,
                type="response",
                name=envelope.name,
                payload=resp_payload,
                correlation_id=envelope.correlation_id,
            )

            try:
                await conn_writer.write_message(resp_env, resp_raw)
            except IPCSendError as exc:
                logger.debug("Failed to send response for '%s': %s", envelope.name, exc)

        elif envelope.type == "event":
            if handler is not None:
                try:
                    await handler(envelope, raw_data)
                except Exception as exc:
                    logger.exception("Handler for event '%s' raised exception", envelope.name)
            else:
                logger.debug("Unsubscribed event '%s' dropped", envelope.name)
