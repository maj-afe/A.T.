# at-ipc — Shared IPC Library for AT

`at-ipc` is the shared, zero-external-dependency IPC library for the AT local voice assistant system. It enables all systemd services to communicate over Unix domain sockets in `/run/at/` using asyncio in Python 3.12+.

---

## Features

- **Asynchronous Unix Domain Sockets:** High-performance local IPC (~50–100 µs latency).
- **Dual Wire Formats:**
  - Standard JSON control envelopes (newline-delimited).
  - High-throughput raw binary payloads (JSON header + N raw bytes) for audio buffers and PCM streams.
- **Request / Response Correlation:** UUID-based correlation tracking with per-call timeouts.
- **Strict FIFO Ordering:** Per-connection message processing is guaranteed in order.
- **Real-Time Audio Fanout:** Concurrent `broadcast()` with `drop_on_backpressure` mode to ensure slow clients never stall real-time pipelines.
- **Lifecycle & Readiness:** Automatic `ready` protocol emission and sub-millisecond disconnect detection via kernel EOF propagation.
- **Zero Dependencies:** Pure Python standard library (`asyncio`, `json`, `uuid`, `datetime`, `pathlib`, `logging`, `socket`, `signal`).

---

## Wire Protocol

### 1. Control Envelope (JSON)

Every control message is a single newline-terminated (`\n`) JSON line:

```json
{
  "msg_id": "4b5d63f2-8927-4a0e-9fa9-6a9bcce86047",
  "correlation_id": "18c1fa3b-9e67-4bb9-92fe-49b01f9e2b02",
  "from": "loop",
  "to": "tts",
  "type": "command",
  "name": "speak",
  "timestamp": "2026-10-20T10:00:00.000000+00:00",
  "payload": {"text": "Good morning, sir."}
}
```

### 2. Audio Payload (Raw Bytes)

Audio chunks (e.g., 960-byte 30 ms PCM frames or multi-MB utterances) use the binary format:
A JSON header containing `"bytes": N`, followed by `\n`, followed by exactly `N` raw bytes:

```
{"msg_id":"...","from":"vad","to":"stt","type":"event","name":"audio_buffer","timestamp":"...","payload":{},"bytes":1920000}\n
<exactly 1920000 raw bytes>
```

---

## Usage Examples

### Server

```python
import asyncio
from at_ipc import IPCServer, Envelope

async def main():
    server = IPCServer("pong")

    # Command handler: returning a dict automatically sends a response envelope
    @server.on("ping")
    async def handle_ping(envelope: Envelope, raw_data: bytes | None):
        return {"reply": "pong"}

    # Binary payload handler
    @server.on("audio_chunk")
    async def handle_audio(envelope: Envelope, raw_data: bytes | None):
        print(f"Received {len(raw_data)} audio bytes")

    # Disconnect notification
    @server.on_disconnect
    async def handle_disconnect(peer_name: str | None):
        print(f"Peer disconnected: {peer_name}")

    await server.start()

    # Real-time audio frame broadcast (does not stall on slow clients)
    audio_frame = b"\x00" * 960
    await server.broadcast("frame", payload={}, raw_data=audio_frame, drop_on_backpressure=True)

    # Keep running or stop on shutdown
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

### Client

```python
import asyncio
from at_ipc import IPCClient, Envelope, IPCTimeoutError

async def main():
    client = IPCClient("ping", "pong")
    await client.connect()

    # Wait for the service to be ready
    await client.wait_ready(timeout=5.0)

    # Send command and await correlated response
    try:
        response = await client.send_command("ping", {"count": 1}, timeout=2.0)
        print("Response:", response.payload)
    except IPCTimeoutError as exc:
        print(f"Request timed out (correlation_id: {exc.correlation_id})")

    # Fire-and-forget event
    await client.send_event("status_update", {"status": "ok"})

    # Send audio payload
    audio_data = b"\x01\x02" * 480
    await client.send_event("audio_chunk", {}, raw_data=audio_data)

    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
```

---

## Exception Hierarchy

All library exceptions inherit from `IPCError`:

- `IPCConnectionError`: Connection failed, socket not found, or connect timed out.
- `IPCDisconnectError`: Peer disconnected unexpectedly or closed the connection.
- `IPCTimeoutError`: Command response or read timed out (`correlation_id` attached).
- `IPCProtocolError`: Malformed JSON, truncated payload, or framing error.
- `IPCSendError`: Writing to a closed or broken socket.
- `IPCResponseError`: Remote handler returned an explicit error response (`error_payload` attached).
