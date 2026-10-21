"""
Unit tests for at_ipc.client (IPCClient).
"""

import asyncio
from pathlib import Path
import time
import pytest

from at_ipc.client import IPCClient
from at_ipc.envelope import Envelope, create_envelope
from at_ipc.errors import (
    IPCConnectionError,
    IPCDisconnectError,
    IPCResponseError,
    IPCSendError,
    IPCTimeoutError,
)
from at_ipc.server import IPCServer


@pytest.mark.asyncio
async def test_client_connect_nonexistent_socket(temp_socket_dir: Path):
    """Connecting to a non-existent socket must raise IPCConnectionError within 2 seconds."""
    non_existent = temp_socket_dir / "does_not_exist.sock"
    client = IPCClient("my_module", "target", socket_path=non_existent)

    t_start = time.perf_counter()
    with pytest.raises(IPCConnectionError, match="Failed to connect|Connection refused|Socket not found"):
        await client.connect()
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 2.0


@pytest.mark.asyncio
async def test_client_wait_ready(temp_socket_dir: Path):
    """Client wait_ready must unblock when server emits ready."""
    sock_path = temp_socket_dir / "ready_test.sock"
    server = IPCServer("pong", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    client = IPCClient("ping", "pong", socket_path=sock_path)
    await client.connect()

    # Even if wait_ready is called slightly after connect, it must return immediately
    await asyncio.sleep(0.02)
    await client.wait_ready(timeout=2.0)

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_client_send_command_round_trip(temp_socket_dir: Path):
    """Client sends command, awaits correlated response."""
    sock_path = temp_socket_dir / "cmd_test.sock"
    server = IPCServer("pong", socket_path=sock_path, install_signal_handlers=False)

    @server.on("greet")
    async def greet(env: Envelope, raw: bytes | None):
        return {"greeting": f"Hello {env.payload['name']}"}

    await server.start()

    client = IPCClient("ping", "pong", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    resp = await client.send_command("greet", {"name": "Kate"})
    assert resp.type == "response"
    assert resp.name == "greet"
    assert resp.payload == {"greeting": "Hello Kate"}

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_client_command_timeout(temp_socket_dir: Path):
    """Command timing out must raise IPCTimeoutError carrying correlation_id."""
    sock_path = temp_socket_dir / "timeout_test.sock"
    server = IPCServer("slow_srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("sleepy")
    async def sleepy(env: Envelope, raw: bytes | None):
        await asyncio.sleep(0.5)
        return {"done": True}

    await server.start()

    client = IPCClient("fast_cli", "slow_srv", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    with pytest.raises(IPCTimeoutError) as exc_info:
        await client.send_command("sleepy", {}, timeout=0.05)

    assert exc_info.value.correlation_id is not None
    assert len(exc_info.value.correlation_id) == 36
    # Verify correlation future cleaned up from client._pending
    assert exc_info.value.correlation_id not in client._pending

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_client_handles_error_response(temp_socket_dir: Path):
    """Remote handler errors must raise IPCResponseError carrying correlation_id and payload."""
    sock_path = temp_socket_dir / "err_test.sock"
    server = IPCServer("err_srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("explode")
    async def explode(env: Envelope, raw: bytes | None):
        raise RuntimeError("boom")

    await server.start()

    client = IPCClient("cli", "err_srv", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    with pytest.raises(IPCResponseError) as exc_info:
        await client.send_command("explode", {})

    assert "boom" in str(exc_info.value)
    assert exc_info.value.correlation_id is not None
    assert exc_info.value.error_payload.get("type") == "RuntimeError"

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_client_event_subscription_fifo(temp_socket_dir: Path):
    """Client subscribed events must be dispatched in exact FIFO order."""
    sock_path = temp_socket_dir / "fifo_test.sock"
    server = IPCServer("source", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    client = IPCClient("sub", "source", socket_path=sock_path)
    received_sequence = []
    done_event = asyncio.Event()

    @client.on("step")
    async def handle_step(env: Envelope, raw: bytes | None):
        received_sequence.append(env.payload["seq"])
        if len(received_sequence) == 10:
            done_event.set()

    await client.connect()
    await client.wait_ready()

    for i in range(10):
        await server.broadcast("step", payload={"seq": i})

    await asyncio.wait_for(done_event.wait(), timeout=1.0)
    assert received_sequence == list(range(10))

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_client_server_disconnect_detection(temp_socket_dir: Path):
    """Killing the server must cause client to detect disconnect within 100 ms."""
    sock_path = temp_socket_dir / "kill_srv.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    client = IPCClient("cli", "srv", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    disconnected = asyncio.Event()

    @client.on_disconnect
    async def on_disconnect():
        disconnected.set()

    t_start = time.perf_counter()
    await server.stop()

    await asyncio.wait_for(disconnected.wait(), timeout=0.1)
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 0.1
    assert not client.is_connected

    await client.close()


class SlowStartServer(IPCServer):
    """Server that binds its socket first, but delays readiness until initialized."""

    READY_DELAY = 0.15

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self._running = True
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        # Window where the socket accepts connections but the service has not
        # finished initializing and must not advertise readiness yet.
        await asyncio.sleep(self.READY_DELAY)
        self._ready = True

    async def _handle_client(self, reader, writer):
        while not self._ready:
            await asyncio.sleep(0.005)
        await super()._handle_client(reader, writer)


@pytest.mark.asyncio
async def test_ready_race_blocks_until_server_ready(temp_socket_dir: Path):
    # Catches: the ready event being sampled instead of latched when
    # wait_ready() is called while the server is still starting up.
    sock_path = temp_socket_dir / "ready_race.sock"
    server = SlowStartServer(
        "slow_srv", socket_path=sock_path, install_signal_handlers=False
    )
    start_task = asyncio.create_task(server.start())

    # Wait only for the bind (socket file), not for readiness: the server is
    # still inside its startup delay when the client connects below.
    for _ in range(100):
        if sock_path.exists():
            break
        await asyncio.sleep(0.001)
    assert sock_path.exists()

    client = IPCClient("early_cli", "slow_srv", socket_path=sock_path)
    await client.connect()

    t_start = time.monotonic()
    await client.wait_ready(timeout=2.0)
    elapsed = time.monotonic() - t_start

    assert 0.05 <= elapsed <= 0.5, f"wait_ready took {elapsed * 1000:.0f} ms"
    assert server.is_ready
    assert client.is_connected

    await start_task
    await client.close()
    await server.stop()
