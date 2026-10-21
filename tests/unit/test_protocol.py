"""
Unit tests for at_ipc.protocol: wire framing, ConnectionWriter, and read_message.
"""

import asyncio
import json
from pathlib import Path
import time
import pytest

from at_ipc.envelope import create_envelope, envelope_to_dict
from at_ipc.errors import (
    IPCDisconnectError,
    IPCProtocolError,
    IPCSendError,
    IPCTimeoutError,
)
from at_ipc.protocol import ConnectionWriter, read_message


@pytest.mark.asyncio
async def test_json_only_round_trip(temp_socket_dir: Path):
    """Test transmitting and reading a JSON-only message over Unix socket."""
    sock_path = temp_socket_dir / "proto1.sock"
    server_ready = asyncio.Event()
    received_envelope = None

    async def client_handler(reader, writer):
        nonlocal received_envelope
        env, raw = await read_message(reader)
        received_envelope = env
        assert raw is None
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)

    send_env = create_envelope(
        from_module="loop",
        to_module="tts",
        type="command",
        name="speak",
        payload={"text": "Yes, sir."},
    )
    await cw.write_message(send_env)

    await asyncio.sleep(0.05)
    cw.close()
    await cw.wait_closed()
    server.close()
    await server.wait_closed()

    assert received_envelope is not None
    assert received_envelope.name == "speak"
    assert received_envelope.payload == {"text": "Yes, sir."}


@pytest.mark.asyncio
async def test_binary_payload_round_trip(temp_socket_dir: Path):
    """Test transmitting 960-byte PCM frame and 2 MB audio buffer byte-for-byte."""
    sock_path = temp_socket_dir / "proto2.sock"

    messages_to_send = [
        (b"\x01\x02" * 480, "pcm_frame"),  # 960 bytes
        (b"A" * (2 * 1024 * 1024), "large_audio"),  # 2 MB
    ]
    received_messages = []

    async def client_handler(reader, writer):
        for _ in range(len(messages_to_send)):
            env, raw = await read_message(reader)
            received_messages.append((env, raw))
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)

    for data, name in messages_to_send:
        env = create_envelope(
            from_module="audio_source",
            to_module="vad",
            type="event",
            name=name,
            payload={"size": len(data)},
        )
        await cw.write_message(env, raw_data=data)

    await asyncio.sleep(0.05)
    cw.close()
    await cw.wait_closed()
    server.close()
    await server.wait_closed()

    assert len(received_messages) == len(messages_to_send)
    for (orig_data, orig_name), (rec_env, rec_data) in zip(
        messages_to_send, received_messages
    ):
        assert rec_env.name == orig_name
        assert rec_data == orig_data


@pytest.mark.asyncio
async def test_eof_detection_raises_disconnect(temp_socket_dir: Path):
    """EOF when peer closes socket must immediately raise IPCDisconnectError."""
    sock_path = temp_socket_dir / "proto3.sock"

    async def client_handler(reader, writer):
        # Read until client closes
        with pytest.raises(IPCDisconnectError, match="EOF"):
            await read_message(reader)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.close()
    await writer.wait_closed()

    await asyncio.sleep(0.05)
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_malformed_json_raises_protocol_error(temp_socket_dir: Path):
    """Malformed JSON line must raise IPCProtocolError without hanging."""
    sock_path = temp_socket_dir / "proto4.sock"

    async def client_handler(reader, writer):
        with pytest.raises(IPCProtocolError, match="Malformed message header"):
            await read_message(reader)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    writer.write(b"this is not valid json\n")
    await writer.drain()

    await asyncio.sleep(0.05)
    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_truncated_payload_raises_protocol_error(temp_socket_dir: Path):
    """Header indicating N bytes when peer sends fewer and closes must raise IPCProtocolError."""
    sock_path = temp_socket_dir / "proto5.sock"

    async def client_handler(reader, writer):
        with pytest.raises(IPCProtocolError, match="Truncated: expected 1000 bytes"):
            await read_message(reader)
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    env = create_envelope(
        from_module="a",
        to_module="b",
        type="event",
        name="test",
        bytes_length=1000,
    )
    cw = ConnectionWriter(writer)
    # Write header with bytes: 1000, but only send 50 bytes then close
    header_line = (
        b'{"msg_id":"m1","correlation_id":"","from":"a","to":"b","type":"event",'
        b'"name":"test","timestamp":"2026-10-20T00:00:00+00:00","payload":{},"bytes":1000}\n'
    )
    writer.write(header_line)
    writer.write(b"x" * 50)
    await writer.drain()
    writer.close()
    await writer.wait_closed()

    await asyncio.sleep(0.05)
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_connection_writer_locking(temp_socket_dir: Path):
    """Concurrent writes via ConnectionWriter must serialize and not interleave."""
    sock_path = temp_socket_dir / "proto6.sock"
    received_count = 0

    async def client_handler(reader, writer):
        nonlocal received_count
        while True:
            try:
                env, raw = await read_message(reader)
                assert raw == b"DATA"
                received_count += 1
            except IPCDisconnectError:
                break
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(client_handler, path=str(sock_path))

    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    cw = ConnectionWriter(writer)

    async def worker(idx: int):
        env = create_envelope(
            from_module="client",
            to_module="server",
            type="event",
            name=f"evt_{idx}",
        )
        await cw.write_message(env, raw_data=b"DATA")

    # Run 50 concurrent writes
    await asyncio.gather(*[worker(i) for i in range(50)])

    await asyncio.sleep(0.05)
    cw.close()
    await cw.wait_closed()
    server.close()
    await server.wait_closed()

    assert received_count == 50


@pytest.mark.asyncio
async def test_raw_payload_read_timeout_is_three_seconds(temp_socket_dir: Path):
    # Catches: a stalled raw-payload read hanging for 30 s (header timeout)
    # instead of the 3 s RAW_PAYLOAD_READ_TIMEOUT.
    sock_path = temp_socket_dir / "proto7.sock"
    outcome: dict = {}
    done = asyncio.Event()

    async def stalled_client_handler(reader, writer):
        t_start = time.monotonic()
        try:
            await read_message(reader)
            outcome["result"] = "no-exception"
        except IPCTimeoutError as exc:
            outcome["error"] = exc
            outcome["elapsed"] = time.monotonic() - t_start
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assertion
            outcome["unexpected"] = exc
        finally:
            done.set()
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_unix_server(
        stalled_client_handler, path=str(sock_path)
    )
    reader, writer = await asyncio.open_unix_connection(str(sock_path))

    env = create_envelope(
        from_module="stalled_sender",
        to_module="receiver",
        type="event",
        name="raw_payload",
        bytes_length=1_000_000,
    )
    header_line = (
        json.dumps(envelope_to_dict(env), separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    writer.write(header_line)
    await writer.drain()

    # Keep the socket open and send nothing more: this must exercise the
    # payload read timeout, not EOF/truncation.
    await asyncio.wait_for(done.wait(), timeout=10.0)

    assert "unexpected" not in outcome
    assert "result" not in outcome
    assert isinstance(outcome.get("error"), IPCTimeoutError)
    assert 2.5 <= outcome["elapsed"] <= 3.8

    writer.close()
    await writer.wait_closed()
    server.close()
    await server.wait_closed()
