"""
Integration test suite for at-ipc: ping/pong services and full acceptance criteria.

Validates all 12+ acceptance criteria specified in specs/02-ipc.md:
  1. Two services exchange commands and responses
  2. Latency: command/response round-trip within 10 ms
  3. 100 concurrent commands correlate correctly
  4. Server emits ready before first command accepted
  5. Non-existent socket raises clear error within 2 seconds
  6. Server kill -> client disconnect detected within 100 ms
  7. Client kill -> server disconnect detected within 100 ms
  8. 2 MB audio payload transmitted byte-for-byte intact
  9. Malformed JSON logged, server continues serving
 10. Command timeout fires correctly carrying correlation_id
 11. 1000 sequential events delivered without loss
 12. Multiple concurrent clients to same server
 13. Per-connection FIFO ordering guarantee
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
    IPCError,
    IPCProtocolError,
    IPCTimeoutError,
)
from at_ipc.protocol import ConnectionWriter, read_message
from at_ipc.server import IPCServer


@pytest.mark.asyncio
async def test_ping_pong_round_trip_latency(temp_socket_dir: Path):
    """
    Acceptance Criterion 1 & 2:
    Ping sends command, pong responds, ping receives response within 10 ms.
    """
    sock_path = temp_socket_dir / "pong.sock"
    pong_server = IPCServer("pong", socket_path=sock_path, install_signal_handlers=False)

    @pong_server.on("ping")
    async def handle_ping(env: Envelope, raw: bytes | None):
        return {"pong": True, "seq": env.payload.get("seq")}

    await pong_server.start()

    ping_client = IPCClient("ping", "pong", socket_path=sock_path)
    await ping_client.connect()
    await ping_client.wait_ready()

    # Warm-up call
    await ping_client.send_command("ping", {"seq": 0})

    # Benchmark round-trip
    t_start = time.perf_counter()
    resp = await ping_client.send_command("ping", {"seq": 1})
    t_elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    assert resp.type == "response"
    assert resp.payload == {"pong": True, "seq": 1}
    assert t_elapsed_ms < 10.0, f"Expected round-trip < 10 ms, got {t_elapsed_ms:.2f} ms"

    await ping_client.close()
    await pong_server.stop()


@pytest.mark.asyncio
async def test_100_concurrent_commands_correlation(temp_socket_dir: Path):
    """
    Acceptance Criterion 3:
    Correlation IDs match request to response under 100 concurrent in-flight requests.
    """
    sock_path = temp_socket_dir / "concurrency.sock"
    server = IPCServer("echo_server", socket_path=sock_path, install_signal_handlers=False)

    @server.on("echo")
    async def handle_echo(env: Envelope, raw: bytes | None):
        # Add slight jitter to test asynchronous response delivery
        await asyncio.sleep(0.001 * (env.payload["id"] % 5))
        return {"id": env.payload["id"], "msg_id": env.msg_id}

    await server.start()

    client = IPCClient("caller", "echo_server", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    async def send_worker(i: int):
        resp = await client.send_command("echo", {"id": i})
        assert resp.payload["id"] == i
        return resp.correlation_id

    # Launch 100 concurrent in-flight requests
    results = await asyncio.gather(*[send_worker(i) for i in range(100)])
    assert len(results) == 100
    assert len(set(results)) == 100  # All correlation IDs must be unique

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_ready_emitted_before_first_command(temp_socket_dir: Path):
    """
    Acceptance Criterion 4:
    Server emits ready before accepting any commands.
    """
    sock_path = temp_socket_dir / "ready_order.sock"
    server = IPCServer("service", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    # Raw connection to inspect exact sequence of bytes arriving on socket
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)

    first_msg, _ = await read_message(reader)
    assert first_msg.name == "ready"
    assert first_msg.type == "event"

    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_connect_nonexistent_socket_error_within_2s(temp_socket_dir: Path):
    """
    Acceptance Criterion 5:
    Client connecting to a non-existent socket raises a clear error within 2 seconds.
    """
    bad_sock = temp_socket_dir / "ghost.sock"
    client = IPCClient("tester", "ghost", socket_path=bad_sock)

    t_start = time.perf_counter()
    with pytest.raises(IPCConnectionError) as exc_info:
        await client.connect()
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 2.0
    assert "ghost.sock" in str(exc_info.value)


@pytest.mark.asyncio
async def test_server_kill_client_detects_within_100ms(temp_socket_dir: Path):
    """
    Acceptance Criterion 6:
    Killing the server causes client to detect disconnect within 100 ms.
    """
    sock_path = temp_socket_dir / "server_death.sock"
    server = IPCServer("fragile_server", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    client = IPCClient("client", "fragile_server", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    disconnect_detected = asyncio.Event()

    @client.on_disconnect
    async def on_disconnect():
        disconnect_detected.set()

    t_start = time.perf_counter()
    # Stop/kill server
    await server.stop()

    await asyncio.wait_for(disconnect_detected.wait(), timeout=0.1)
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 0.1, f"Expected disconnect < 100 ms, took {t_elapsed * 1000:.1f} ms"
    assert not client.is_connected

    await client.close()


@pytest.mark.asyncio
async def test_client_kill_server_detects_within_100ms(temp_socket_dir: Path):
    """
    Acceptance Criterion 7:
    Killing the client causes server to detect disconnect within 100 ms.
    """
    sock_path = temp_socket_dir / "client_death.sock"
    server = IPCServer("server", socket_path=sock_path, install_signal_handlers=False)

    disconnect_detected = asyncio.Event()
    disconnected_peer = None

    @server.on_disconnect
    async def on_disconnect(peer: str | None):
        nonlocal disconnected_peer
        disconnected_peer = peer
        disconnect_detected.set()

    await server.start()

    client = IPCClient("doomed_client", "server", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    # Send message so server knows peer identity
    await client.send_event("identify", {"status": "alive"})
    await asyncio.sleep(0.01)

    t_start = time.perf_counter()
    await client.close()

    await asyncio.wait_for(disconnect_detected.wait(), timeout=0.1)
    t_elapsed = time.perf_counter() - t_start

    assert t_elapsed < 0.1, f"Expected disconnect < 100 ms, took {t_elapsed * 1000:.1f} ms"
    assert disconnected_peer == "doomed_client"

    await server.stop()


@pytest.mark.asyncio
async def test_2mb_audio_payload_intact(temp_socket_dir: Path):
    """
    Acceptance Criterion 8:
    Audio payload of 2 MB transmits and receives byte-for-byte intact.
    """
    sock_path = temp_socket_dir / "audio_2mb.sock"
    server = IPCServer("stt_server", socket_path=sock_path, install_signal_handlers=False)

    received_data = None
    data_received_event = asyncio.Event()

    @server.on("transcribe")
    async def handle_transcribe(env: Envelope, raw: bytes | None):
        nonlocal received_data
        received_data = raw
        data_received_event.set()
        return {"samples": len(raw or b"")}

    await server.start()

    client = IPCClient("vad_client", "stt_server", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    # Generate 2 MB of deterministic binary audio data
    two_mb_payload = bytes(i % 256 for i in range(2 * 1024 * 1024))
    assert len(two_mb_payload) == 2 * 1024 * 1024

    resp = await client.send_command("transcribe", {}, raw_data=two_mb_payload)
    assert resp.payload["samples"] == 2 * 1024 * 1024

    await asyncio.wait_for(data_received_event.wait(), timeout=3.0)
    assert received_data == two_mb_payload

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_malformed_json_server_resilience(temp_socket_dir: Path):
    """
    Acceptance Criterion 9:
    Malformed JSON does not crash the server — logs error, continues serving.
    """
    sock_path = temp_socket_dir / "resilience.sock"
    server = IPCServer("stable_srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("ping")
    async def handle_ping(env: Envelope, raw: bytes | None):
        return {"status": "healthy"}

    await server.start()

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)
    await read_message(reader)  # ready

    # Inject corrupt non-JSON lines
    writer.write(b"NOT_VALID_JSON_AT_ALL\n")
    writer.write(b"{broken json\n")
    await writer.drain()

    # Send valid command right after corrupt lines
    valid_cmd = create_envelope(
        from_module="c", to_module="stable_srv", type="command", name="ping", correlation_id="c1"
    )
    await cw.write_message(valid_cmd)

    resp, _ = await read_message(reader)
    assert resp.type == "response"
    assert resp.correlation_id == "c1"
    assert resp.payload == {"status": "healthy"}

    cw.close()
    await cw.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_command_timeout_fires_with_correlation_id(temp_socket_dir: Path):
    """
    Acceptance Criterion 10:
    Command timeout fires correctly, raises exception carrying the correlation_id.
    """
    sock_path = temp_socket_dir / "timeout_corr.sock"
    server = IPCServer("srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("stalled_cmd")
    async def handle_stalled(env: Envelope, raw: bytes | None):
        await asyncio.sleep(1.0)
        return {}

    await server.start()

    client = IPCClient("cli", "srv", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    with pytest.raises(IPCTimeoutError) as exc_info:
        await client.send_command("stalled_cmd", {}, timeout=0.05)

    assert exc_info.value.correlation_id is not None
    assert len(exc_info.value.correlation_id) == 36

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_1000_sequential_events_delivered_without_loss(temp_socket_dir: Path):
    """
    Acceptance Criterion 11:
    1000 sequential events delivered without loss.
    """
    sock_path = temp_socket_dir / "events_1000.sock"
    server = IPCServer("receiver", socket_path=sock_path, install_signal_handlers=False)

    received_events = []
    completion_event = asyncio.Event()

    @server.on("tick")
    async def handle_tick(env: Envelope, raw: bytes | None):
        seq = env.payload["seq"]
        received_events.append(seq)
        if len(received_events) == 1000:
            completion_event.set()

    await server.start()

    client = IPCClient("sender", "receiver", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    for i in range(1000):
        await client.send_event("tick", {"seq": i})

    await asyncio.wait_for(completion_event.wait(), timeout=5.0)

    assert len(received_events) == 1000
    assert received_events == list(range(1000))

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_per_connection_fifo_guarantee(temp_socket_dir: Path):
    """
    Acceptance Criterion: Per-connection FIFO ordering is guaranteed.
    """
    sock_path = temp_socket_dir / "fifo_order.sock"
    server = IPCServer("conductor", socket_path=sock_path, install_signal_handlers=False)

    processed_order = []
    done_event = asyncio.Event()

    @server.on("step")
    async def handle_step(env: Envelope, raw: bytes | None):
        step_id = env.payload["step"]
        # Intentionally sleep longer on early steps to verify they aren't overtaken
        if step_id == 1:
            await asyncio.sleep(0.02)
        processed_order.append(step_id)
        if len(processed_order) == 5:
            done_event.set()

    await server.start()

    client = IPCClient("caller", "conductor", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    for s in [1, 2, 3, 4, 5]:
        await client.send_event("step", {"step": s})

    await asyncio.wait_for(done_event.wait(), timeout=1.0)
    assert processed_order == [1, 2, 3, 4, 5]

    await client.close()
    await server.stop()


@pytest.mark.asyncio
async def test_multiple_concurrent_clients(temp_socket_dir: Path):
    """
    Acceptance Criterion: Multiple concurrent clients to same server.
    """
    sock_path = temp_socket_dir / "multi_clients.sock"
    server = IPCServer("shared_srv", socket_path=sock_path, install_signal_handlers=False)

    @server.on("get_client_id")
    async def get_id(env: Envelope, raw: bytes | None):
        return {"client": env.from_module}

    await server.start()

    num_clients = 10
    clients = [
        IPCClient(f"client_{i}", "shared_srv", socket_path=sock_path)
        for i in range(num_clients)
    ]

    for c in clients:
        await c.connect()
        await c.wait_ready()

    async def client_action(c: IPCClient):
        resp = await c.send_command("get_client_id")
        assert resp.payload["client"] == c.module_name

    await asyncio.gather(*[client_action(c) for c in clients])

    for c in clients:
        await c.close()
    await server.stop()


@pytest.mark.asyncio
async def test_concurrent_commands_execute_strictly_fifo(temp_socket_dir: Path):
    # Catches: interleaved handler execution (and reordered responses) when
    # 20 commands are dispatched concurrently over one connection.
    sock_path = temp_socket_dir / "fifo_concurrent.sock"
    server = IPCServer("fifo_srv", socket_path=sock_path, install_signal_handlers=False)

    exec_start_log: list[int] = []
    exec_end_log: list[int] = []

    @server.on("cmd")
    async def handle_cmd(env: Envelope, raw: bytes | None):
        i = env.payload["i"]
        exec_start_log.append(i)
        await asyncio.sleep(0.005)
        exec_end_log.append(i)
        return {"i": i}

    await server.start()

    client = IPCClient("fifo_cli", "fifo_srv", socket_path=sock_path)
    await client.connect()
    await client.wait_ready()

    responses = await asyncio.gather(
        *[client.send_command("cmd", {"i": i}) for i in range(20)]
    )

    assert [r.payload["i"] for r in responses] == list(range(20))
    assert exec_start_log == list(range(20))
    assert exec_end_log == exec_start_log

    await client.close()
    await server.stop()
