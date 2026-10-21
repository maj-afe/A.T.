"""
Adversarial tests for IPCServer.broadcast: backpressure drop, slow-client
eviction, and exactly-once disconnect notification.
"""

import asyncio
from pathlib import Path

import pytest

from at_ipc.envelope import create_envelope
from at_ipc.errors import IPCDisconnectError
from at_ipc.protocol import ConnectionWriter, read_message
from at_ipc.server import IPCServer

FRAME = b"x" * 100_000
FRAME_COUNT = 40


class TrackedServer(IPCServer):
    """IPCServer that records the server-side StreamWriter for each client."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_writers: list[asyncio.StreamWriter] = []

    async def _handle_client(self, reader, writer):
        self.server_writers.append(writer)
        await super()._handle_client(reader, writer)


async def _connect(path: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect and give the server's _handle_client time to register the client."""
    reader, writer = await asyncio.open_unix_connection(path)
    await asyncio.sleep(0.01)
    return reader, writer


async def _identify(writer: asyncio.StreamWriter, module: str) -> None:
    """Announce a read-only subscriber so it is nameable in disconnect callbacks."""
    await ConnectionWriter(writer).write_message(
        create_envelope(
            from_module=module,
            to_module="source",
            type="event",
            name="hello",
            payload={"module": module},
        )
    )
    await asyncio.sleep(0.01)


async def _read_frames(
    reader: asyncio.StreamReader, count: int, sink: list[bytes]
) -> None:
    """Read until `count` frames have been collected (ignoring control events)."""
    try:
        while len(sink) < count:
            envelope, raw = await read_message(reader)
            if envelope.name == "frame":
                sink.append(raw)
    except IPCDisconnectError:
        pass


async def _drain_frames(reader: asyncio.StreamReader, sink: list[bytes]) -> None:
    """Collect every frame currently deliverable, then stop."""
    try:
        while True:
            envelope, raw = await asyncio.wait_for(
                read_message(reader), timeout=0.2
            )
            if envelope.name == "frame":
                sink.append(raw)
    except (asyncio.TimeoutError, IPCDisconnectError):
        pass


@pytest.mark.asyncio
async def test_broadcast_drop_on_backpressure_drops_frames_keeps_client(
    temp_socket_dir: Path,
):
    # Catches: drop_on_backpressure=True silently buffering every frame anyway,
    # because the frame is written to the transport before the timeout is checked.
    sock_path = temp_socket_dir / "drop.sock"
    server = TrackedServer("source", socket_path=sock_path, install_signal_handlers=False)
    await server.start()

    slow_reader, slow_writer = await _connect(str(sock_path))
    await _identify(slow_writer, "slow_client")

    fast_clients = [await _connect(str(sock_path)) for _ in range(2)]

    fast_frames: list[list[bytes]] = [[], []]
    readers = [
        asyncio.create_task(
            _read_frames(fast_clients[0][0], FRAME_COUNT, fast_frames[0])
        ),
        asyncio.create_task(
            _read_frames(fast_clients[1][0], FRAME_COUNT, fast_frames[1])
        ),
    ]

    for _ in range(FRAME_COUNT):
        await server.broadcast("frame", raw_data=FRAME, drop_on_backpressure=True)

    await asyncio.wait_for(asyncio.gather(*readers), timeout=10.0)
    assert [len(frames) for frames in fast_frames] == [FRAME_COUNT, FRAME_COUNT]

    slow_frames: list[bytes] = []
    await _drain_frames(slow_reader, slow_frames)
    assert len(slow_frames) < FRAME_COUNT

    slow_server_writer = server.server_writers[0]
    assert not slow_server_writer.is_closing()
    assert id(slow_server_writer) in server._clients
    assert server._client_peers.get(id(slow_server_writer)) == "slow_client"

    for _, writer in fast_clients:
        writer.close()
    slow_writer.close()
    await server.stop()


@pytest.mark.asyncio
async def test_broadcast_eviction_fires_disconnect_exactly_once(
    temp_socket_dir: Path,
):
    # Catches: duplicate on_disconnect callback on eviction (broadcast and
    # _handle_client.finally both firing) and "unknown" peer for a silent client.
    sock_path = temp_socket_dir / "evict.sock"
    server = TrackedServer("source", socket_path=sock_path, install_signal_handlers=False)

    disconnects: list[str | None] = []
    evicted = asyncio.Event()

    @server.on_disconnect
    async def on_disconnect(peer: str | None):
        disconnects.append(peer)
        evicted.set()

    await server.start()

    slow_reader, slow_writer = await _connect(str(sock_path))
    await _identify(slow_writer, "slow_client")

    fast_clients = [await _connect(str(sock_path)) for _ in range(2)]

    fast_frames: list[list[bytes]] = [[], []]
    readers = [
        asyncio.create_task(
            _read_frames(fast_clients[0][0], FRAME_COUNT, fast_frames[0])
        ),
        asyncio.create_task(
            _read_frames(fast_clients[1][0], FRAME_COUNT, fast_frames[1])
        ),
    ]

    for _ in range(FRAME_COUNT):
        await server.broadcast("frame", raw_data=FRAME, drop_on_backpressure=False)

    await asyncio.wait_for(evicted.wait(), timeout=5.0)
    # Allow a duplicate callback (the bug) time to arrive before asserting.
    await asyncio.sleep(0.1)

    assert disconnects == ["slow_client"]

    await asyncio.wait_for(asyncio.gather(*readers), timeout=10.0)
    assert [len(frames) for frames in fast_frames] == [FRAME_COUNT, FRAME_COUNT]

    slow_server_writer = server.server_writers[0]
    assert id(slow_server_writer) not in server._clients

    for _, writer in fast_clients:
        writer.close()
    slow_writer.close()
    await server.stop()
