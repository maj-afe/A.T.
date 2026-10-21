"""
Unit tests for at_ipc.server (IPCServer).
"""

import asyncio
import os
from pathlib import Path
import stat
import time
import pytest

from at_ipc.envelope import Envelope, create_envelope
from at_ipc.errors import IPCDisconnectError, IPCError
from at_ipc.protocol import ConnectionWriter, read_message
from at_ipc.server import IPCServer


@pytest.mark.asyncio
async def test_server_socket_creation_and_permissions(temp_socket_dir: Path):
    """Server must bind socket, set 0600 permissions, and unlink on stop."""
    sock_path = temp_socket_dir / "srv1.sock"
    server = IPCServer("test_srv", socket_path=sock_path, install_signal_handlers=False)

    await server.start()
    assert sock_path.exists()

    # Permissions check: 0600
    mode = os.stat(sock_path).st_mode
    assert stat.S_IMODE(mode) == 0o600

    await server.stop()
    assert not sock_path.exists()


@pytest.mark.asyncio
async def test_server_emits_ready_on_connect(temp_socket_dir: Path):
    """Server must emit ready event immediately when a client connects."""
    sock_path = temp_socket_dir / "srv2.sock"
    server = IPCServer("test_srv", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    env, raw = await read_message(reader)

    assert env.name == "ready"
    assert env.type == "event"
    assert env.from_module == "test_srv"

    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_server_command_auto_response(temp_socket_dir: Path):
    """Server must execute handler and send response correlated with correlation_id."""
    sock_path = temp_socket_dir / "srv3.sock"
    server = IPCServer("pong", socket_path=sock_path, install_signal_handlers=False)

    @server.on("ping")
    async def ping_handler(envelope: Envelope, raw_data: bytes | None):
        return {"pong": envelope.payload.get("count", 0) + 1}

    @server.on("echo_audio")
    async def audio_handler(envelope: Envelope, raw_data: bytes | None):
        # Return tuple of (dict, bytes)
        return {"echoed": True}, raw_data

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)

    # Discard initial ready event
    ready_env, _ = await read_message(reader)
    assert ready_env.name == "ready"

    # Send ping command
    cmd1 = create_envelope(
        from_module="ping",
        to_module="pong",
        type="command",
        name="ping",
        payload={"count": 5},
        correlation_id="corr-555",
    )
    await cw.write_message(cmd1)

    resp1, raw1 = await read_message(reader)
    assert resp1.type == "response"
    assert resp1.name == "ping"
    assert resp1.correlation_id == "corr-555"
    assert resp1.payload == {"pong": 6}
    assert raw1 is None

    # Send echo_audio command with binary payload
    audio_bytes = b"\x05\x06\x07" * 100
    cmd2 = create_envelope(
        from_module="ping",
        to_module="pong",
        type="command",
        name="echo_audio",
        payload={},
        correlation_id="corr-666",
    )
    await cw.write_message(cmd2, raw_data=audio_bytes)

    resp2, raw2 = await read_message(reader)
    assert resp2.type == "response"
    assert resp2.correlation_id == "corr-666"
    assert resp2.payload == {"echoed": True}
    assert raw2 == audio_bytes

    cw.close()
    await cw.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_server_error_responses(temp_socket_dir: Path):
    """Missing handler and handler exceptions must return error response."""
    sock_path = temp_socket_dir / "srv4.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("failing_cmd")
    async def failing_handler(env: Envelope, raw: bytes | None):
        raise ValueError("Something broke inside handler")

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)
    await read_message(reader)  # ready

    # 1. Unregistered command
    cmd_unknown = create_envelope(
        from_module="c", to_module="srv", type="command", name="nonexistent", correlation_id="c1"
    )
    await cw.write_message(cmd_unknown)
    resp1, _ = await read_message(reader)
    assert resp1.type == "error"
    assert resp1.correlation_id == "c1"
    assert "No handler registered" in resp1.payload["error"]

    # 2. Failing handler
    cmd_fail = create_envelope(
        from_module="c", to_module="srv", type="command", name="failing_cmd", correlation_id="c2"
    )
    await cw.write_message(cmd_fail)
    resp2, _ = await read_message(reader)
    assert resp2.type == "error"
    assert resp2.correlation_id == "c2"
    assert "Something broke inside handler" in resp2.payload["error"]

    cw.close()
    await cw.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_server_resilience_to_malformed_json(temp_socket_dir: Path):
    """Malformed JSON line must not crash the server; server serves next valid message."""
    sock_path = temp_socket_dir / "srv5.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("valid_cmd")
    async def handle_valid(env: Envelope, raw: bytes | None):
        return {"status": "ok"}

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)
    await read_message(reader)  # ready

    # Send garbage newline-terminated string
    writer.write(b"NOT_JSON_GARBAGE\n")
    await writer.drain()

    # Immediately follow with a valid command
    cmd = create_envelope(
        from_module="c", to_module="srv", type="command", name="valid_cmd", correlation_id="c99"
    )
    await cw.write_message(cmd)

    resp, _ = await read_message(reader)
    assert resp.type == "response"
    assert resp.correlation_id == "c99"
    assert resp.payload == {"status": "ok"}

    cw.close()
    await cw.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_server_disconnect_callback_latency(temp_socket_dir: Path):
    """Killing the client must cause server to detect disconnect within 100 ms."""
    sock_path = temp_socket_dir / "srv6.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)

    disconnected_event = asyncio.Event()
    disconnected_peer = None

    @server.on_disconnect
    async def on_disconnect(peer: str | None):
        nonlocal disconnected_peer
        disconnected_peer = peer
        disconnected_event.set()

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)
    await read_message(reader)  # ready

    # Identify by sending an event
    env = create_envelope(from_module="my_test_client", to_module="srv", type="event", name="hello")
    await cw.write_message(env)
    await asyncio.sleep(0.01)

    t_start = time.perf_counter()
    writer.close()
    await writer.wait_closed()

    await asyncio.wait_for(disconnected_event.wait(), timeout=0.1)
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 0.1  # Within 100 ms requirement
    assert disconnected_peer == "my_test_client"

    await server.stop()


@pytest.mark.asyncio
async def test_server_broadcast_fanout(temp_socket_dir: Path):
    """Broadcast must deliver events concurrently to all connected clients."""
    sock_path = temp_socket_dir / "srv7.sock"
    server = IPCServer("audio_source", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    # Connect 3 clients
    clients = []
    for _ in range(3):
        r, w = await asyncio.open_unix_connection(str(sock_path))
        await read_message(r)  # ready
        clients.append((r, w))

    # Broadcast audio frame
    pcm_bytes = b"\x11" * 960
    await server.broadcast("pcm_frame", payload={"frame_no": 1}, raw_data=pcm_bytes)

    for r, w in clients:
        env, raw = await read_message(r)
        assert env.name == "pcm_frame"
        assert env.from_module == "audio_source"
        assert env.to_module == "*"
        assert raw == pcm_bytes
        w.close()
        await w.wait_closed()

    await server.stop()


@pytest.mark.asyncio
async def test_eof_terminates_read_loop_after_one_iteration(
    temp_socket_dir: Path, monkeypatch
):
    # Catches: the read loop re-entering after EOF (busy-spin or leak) instead
    # of breaking on IPCDisconnectError after a single read attempt.
    import at_ipc.server as server_module

    sock_path = temp_socket_dir / "eof_loop.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)

    read_iterations = 0
    real_read_message = server_module.read_message

    async def counting_read_message(reader):
        nonlocal read_iterations
        read_iterations += 1
        return await real_read_message(reader)

    monkeypatch.setattr(server_module, "read_message", counting_read_message)

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    await asyncio.sleep(0.05)  # let _handle_client enter the read loop
    writer.close()
    await writer.wait_closed()

    await asyncio.sleep(0.2)
    assert read_iterations == 1

    # Counter must stay put: the loop must not spin or resume after EOF.
    await asyncio.sleep(0.1)
    assert read_iterations == 1
    assert len(server._clients) == 0

    await server.stop()
