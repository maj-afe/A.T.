### User Directives Applied

1. **Broadcast write timeout:** Changed from 1 s to `100 ms` default. Added `drop_on_backpressure` mode (default `False`, audio-source will use `True`). Stalling the pipeline is prevented; slow subscribers in drop mode simply miss the frame, log the event, and continue.
* *Lands in:* Section 2.2 (`_constants.py`), Section 2.5 (`IPCServer.broadcast` flow), Section 6 (Timeout Summary), Section 10.


2. **FIFO ordering:** Reverted concurrent dispatch. Per-connection FIFO is mandatory. Implemented via inline awaiting in the read loop (`await handle(msg)` directly, no `create_task` per message).
* *Lands in:* Section 2.5 (`_handle_client` flow), Section 2.6 (`IPCClient._read_loop` flow), Section 10.


3. **Raw-byte read timeout:** Changed raw payload `readexactly` timeout from 30 seconds to `3 seconds` to avoid hanging on dead senders.
* *Lands in:* Section 2.2 (`_constants.py`), Section 2.4 (`read_message` flow), Section 6 (Timeout Summary), Section 10.



---

# at-ipc — Final Implementation Plan

## 1. File Structure

```text
src/at_ipc/
├── __init__.py          # Public API re-exports
├── envelope.py          # Message envelope: dataclass, encode, decode, validation
├── protocol.py          # Wire protocol: framing, reading, writing, ConnectionWriter
├── server.py            # IPCServer: listen, accept, dispatch, ready, broadcast, shutdown
├── client.py            # IPCClient: connect, send_command, send_event, subscribe
├── errors.py            # Exception hierarchy
└── _constants.py        # Shared constants (timeouts, size thresholds, socket dir)

tests/
├── unit/
│   ├── test_envelope.py
│   ├── test_protocol.py
│   ├── test_server.py
│   └── test_client.py
├── integration/
│   └── test_ping_pong.py
└── conftest.py

```

Five source modules, each under 300 lines. `envelope.py` and `protocol.py` are pure logic — easy to test without real sockets. `server.py` and `client.py` own all asyncio I/O. `errors.py` is tiny but avoids circular imports.

---

## 2. Module Breakdown

### 2.1 `errors.py` — Exception Hierarchy

```python
IPCError (base)
├── IPCConnectionError    — socket connect failure, target not found, connection timeout
├── IPCDisconnectError    — peer dropped unexpectedly, clean EOF
├── IPCTimeoutError       — command response timeout (carries correlation_id)
├── IPCProtocolError      — malformed envelope, truncated payload, bad JSON
├── IPCSendError          — write to closed/broken socket
└── IPCResponseError      — remote returned error envelope (carries correlation_id + error_payload)

```

Key attributes:

* `IPCTimeoutError.correlation_id: str | None` — identifies which request timed out
* `IPCResponseError.correlation_id: str | None` and `error_payload: dict` — carries the remote error details

---

### 2.2 `_constants.py`

```python
from pathlib import Path

DEFAULT_SOCKET_DIR = Path("/run/at")
SOCKET_PERMISSIONS = 0o600
DEFAULT_COMMAND_TIMEOUT = 10.0      # seconds
CONNECTION_TIMEOUT = 2.0            # seconds
HEADER_READ_TIMEOUT = 30.0          # seconds
RAW_PAYLOAD_READ_TIMEOUT = 3.0      # seconds
BROADCAST_WRITE_TIMEOUT = 0.1       # seconds (100 ms)
MAX_JSON_SIZE = 64 * 1024           # 64 KB — spec threshold
MAX_PAYLOAD_SIZE = 10 * 1024 * 1024 # 10 MB — defensive cap on bytes_length
STREAM_READER_LIMIT = 1_048_576     # 1 MB buffer limit for StreamReader
MSG_TYPES = frozenset({"event", "command", "response", "error"})
BROADCAST_TARGET = "*"              # canonical "to" value for broadcast envelopes

```

---

### 2.3 `envelope.py`

**Dataclass: `Envelope**`

| Field | Type | Default | JSON Key |
| --- | --- | --- | --- |
| `msg_id` | `str` | UUID4 | `msg_id` |
| `correlation_id` | `str` | `""` | `correlation_id` |
| `from_module` | `str` | Required | `from` |
| `to_module` | `str` | Required | `to` |
| `type` | `str` | Required | `type` |
| `name` | `str` | Required | `name` |
| `timestamp` | `str` | UTC ISO 8601 | `timestamp` |
| `payload` | `dict` | `{}` | `payload` |
| `bytes_length` | `int | None` | `None` |

`from` is a Python keyword — the field is `from_module`, serialized as `"from"`.

Timestamps use `datetime.datetime.now(datetime.timezone.utc).isoformat()` (Python 3.12 compatible).

**Functions:**

* `create_envelope(from_module, to_module, type, name, payload={}, correlation_id="", bytes_length=None) → Envelope`
* `envelope_to_dict(env: Envelope) → dict` — serializes, mapping field names to JSON keys, omitting `bytes` if None. If the serialized header JSON exceeds `MAX_JSON_SIZE` bytes, raises `IPCProtocolError`.
* `dict_to_envelope(d: dict) → Envelope` — deserializes, validates required fields, raises `IPCProtocolError` on missing/invalid.
* `validate_envelope(env: Envelope) → None` — checks type ∈ MSG_TYPES, msg_id present, timestamp present, bytes_length ≤ MAX_PAYLOAD_SIZE if set.

---

### 2.4 `protocol.py` — Wire Framing

**`ConnectionWriter`** — wraps a `StreamWriter` with an `asyncio.Lock` to guarantee atomic multi-part writes. This is defense-in-depth: in asyncio's single-threaded model, synchronous `write()` calls don't interleave. The lock protects against future refactors that might add `await` points between writes, and serializes `drain()` calls for predictable flow control.

```python
class ConnectionWriter:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer
        self._lock = asyncio.Lock()

    async def write_message(self, envelope: Envelope, raw_data: bytes | None = None) -> None:
        if raw_data is not None:
            envelope.bytes_length = len(raw_data)

        header_line = json.dumps(envelope_to_dict(envelope), separators=(',', ':')).encode('utf-8') + b'\n'

        async with self._lock:
            try:
                self._writer.write(header_line)
                if raw_data is not None:
                    self._writer.write(raw_data)
                await self._writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError) as exc:
                raise IPCSendError(f"Write failed: {exc}") from exc

    def close(self) -> None:
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()

```

**`read_message`** — reads one complete message from a `StreamReader`. Handles EOF, JSON parsing, optional raw bytes.

```python
async def read_message(reader: asyncio.StreamReader) -> tuple[Envelope, bytes | None]:
    # Step 1: Read newline-terminated header line
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=HEADER_READ_TIMEOUT)
    except asyncio.TimeoutError:
        raise IPCTimeoutError("Read timed out")
    except ConnectionResetError:
        raise IPCDisconnectError("Connection reset by peer")
    except ValueError as exc:
        raise IPCProtocolError(f"Line length exceeded limit: {exc}") from exc

    # CRITICAL: EOF check before parsing — prevents 100% CPU spin loop
    if not line:
        raise IPCDisconnectError("Connection closed (EOF)")

    # Step 2: Parse JSON header
    try:
        d = json.loads(line)
        envelope = dict_to_envelope(d)
    except (json.JSONDecodeError, UnicodeDecodeError, IPCProtocolError) as exc:
        raise IPCProtocolError(f"Malformed header: {exc}") from exc

    # Step 3: Read raw payload if specified
    raw_data = None
    if envelope.bytes_length is not None and envelope.bytes_length > 0:
        try:
            raw_data = await asyncio.wait_for(
                reader.readexactly(envelope.bytes_length), timeout=RAW_PAYLOAD_READ_TIMEOUT
            )
        except asyncio.IncompleteReadError as exc:
            raise IPCProtocolError(
                f"Truncated: expected {envelope.bytes_length} bytes, got {len(exc.partial)}"
            ) from exc
        except asyncio.TimeoutError:
            raise IPCTimeoutError(f"Timed out reading {envelope.bytes_length} byte payload")

    return envelope, raw_data

```

---

### 2.5 `server.py` — `IPCServer`

```python
class IPCServer:
    def __init__(
        self,
        module_name: str,
        socket_path: Path | None = None,
        install_signal_handlers: bool = True,
    ) -> None

```

**Public API:**

| Method | Signature | Description |
| --- | --- | --- |
| `on` | `(name: str, handler: Callable) → None` | Register async handler for message name. |
| `on_disconnect` | `(callback: Callable[[str | None], Awaitable[None]]) → None` | Register disconnect callback, receives peer module name. |
| `start` | `async () → None` | Bind socket, start serving, mark ready. |
| `stop` | `async () → None` | Close all connections, cancel tasks, unlink socket. |
| `broadcast` | `async (name: str, payload: dict = {}, raw_data: bytes | None = None, drop_on_backpressure: bool = False) → None` | Send event to all connected clients concurrently. |

**Internal state:**

* `_handlers: dict[str, Callable]`
* `_clients: dict[int, ConnectionWriter]` — `id(writer) → ConnectionWriter`
* `_client_peers: dict[int, str]` — `id(writer) → peer module name`
* `_server: asyncio.Server | None`
* `_ready: bool`
* `_running: bool`
* `_signal_installed: bool`

**`start()` flow:**

1. If `socket_path` exists: attempt connection. If accepted → raise `IPCError("Socket already in use")`. If `ConnectionRefusedError` → stale, unlink.
2. Ensure parent directory exists.
3. Set `_ready = True`, `_running = True`.
4. `asyncio.start_unix_server(self._handle_client, path=self.socket_path, limit=STREAM_READER_LIMIT)`.
5. `os.chmod(self.socket_path, SOCKET_PERMISSIONS)`.
6. If `install_signal_handlers`: `loop.add_signal_handler(SIGTERM, ...)`, set `_signal_installed = True`.

**`_handle_client(reader, writer)` flow:**

1. Wrap: `conn_writer = ConnectionWriter(writer)`.
2. Register: `_clients[id(writer)] = conn_writer`.
3. Send `ready` event unconditionally to this client.
4. Read loop:
```python
while self._running:
    try:
        envelope, raw_data = await read_message(reader)
    except IPCDisconnectError:
        break
    except IPCTimeoutError:
        log.debug("Client read timeout, disconnecting")
        break
    except IPCProtocolError:
        log.warning("Malformed message, ignoring")
        continue  

    _client_peers[id(writer)] = envelope.from_module  

    # CRITICAL: FIFO ordering is guaranteed per (client, server) connection pair.
    # Dispatch inline (no create_task) per User Directive.
    await self._dispatch(envelope, raw_data, conn_writer)

```


5. `finally`: remove from `_clients`, close writer, invoke `_on_disconnect_cb(peer_name)`.

**`_dispatch(envelope, raw_data, conn_writer)` flow:**

1. Look up handler: `handler = self._handlers.get(envelope.name)`.
2. No handler + command → send error response with `correlation_id`.
3. No handler + event → log debug, drop.
4. Handler found:
* `try: result = await handler(envelope, raw_data)`
* If command:
* `result` is `dict` → send response with `payload=result`.
* `result` is `tuple[dict, bytes]` → send response with `payload=result[0]`, `raw_data=result[1]`.
* `result` is `None` → send response with `payload={}`.


* `except Exception as exc:` → log error, send error response if command.



**`broadcast(name, payload={}, raw_data=None, drop_on_backpressure=False)` flow:**

1. Build event envelope: `from_module=self.module_name`, `to_module="*"`, `type="event"`.
2. Fan out concurrently with per-client 100 ms write timeout:
```python
async def _write_to_client(cid: int, cw: ConnectionWriter) -> tuple[int, Exception | None]:
    try:
        await asyncio.wait_for(cw.write_message(envelope, raw_data), timeout=BROADCAST_WRITE_TIMEOUT)
        return (cid, None)
    except asyncio.TimeoutError as exc:
        if drop_on_backpressure:
            return (cid, None)
        return (cid, exc)
    except IPCSendError as exc:
        return (cid, exc)

results = await asyncio.gather(
    *[_write_to_client(cid, cw) for cid, cw in self._clients.items()]
)

for cid, exc in results:
    if exc is not None:
        log.info("Evicting client %s during broadcast: %s", cid, exc)
        cw = self._clients.pop(cid, None)
        if cw:
            cw.close()

```



**`stop()` flow:**

1. Set `_running = False`.
2. If `_signal_installed`: `loop.remove_signal_handler(signal.SIGTERM)`.
3. `_server.close()`, `await _server.wait_closed()`.
4. Close all client `ConnectionWriter`s.
5. Unlink `socket_path`.

---

### 2.6 `client.py` — `IPCClient`

```python
class IPCClient:
    def __init__(
        self,
        module_name: str,
        target_module: str,
        socket_path: Path | None = None,
    ) -> None

```

**Public API:**

| Method | Signature | Description |
| --- | --- | --- |
| `connect` | `async () → None` | Connect with 2s timeout, start background read loop. |
| `close` | `async () → None` | Cancel read loop, fail pending futures, close socket. |
| `send_command` | `async (name: str, payload: dict = {}, *, timeout: float = 10.0, raw_data: bytes | None = None) → Envelope` | Send command, await response. Raises `IPCTimeoutError` or `IPCResponseError`. |
| `send_event` | `async (name: str, payload: dict = {}, raw_data: bytes | None = None) → None` | Fire and forget. No correlation, no response expected. |
| `on` | `(name: str, callback: Callable) → None` | Subscribe to inbound events by name. |
| `on_disconnect` | `(callback: Callable[[], Awaitable[None]]) → None` | Register disconnect callback. |
| `wait_ready` | `async (timeout: float = 10.0) → None` | Block until server sends `ready`. |

**Internal state:**

* `_conn_writer: ConnectionWriter | None`
* `_reader: asyncio.StreamReader | None`
* `_pending: dict[str, asyncio.Future[Envelope]]`
* `_event_handlers: dict[str, list[Callable]]`
* `_on_disconnect_cb: Callable | None`
* `_read_task: asyncio.Task | None`
* `_ready_event: asyncio.Event` — **initialized in `__init__**`
* `_connected: bool`

**`connect()` flow:**

Wrap `asyncio.open_unix_connection(str(self.socket_path), limit=STREAM_READER_LIMIT)` in a `try/except` block and await it with `CONNECTION_TIMEOUT`.
Catch `FileNotFoundError`, `ConnectionRefusedError`, `PermissionError`, and `asyncio.TimeoutError` and re-raise as `IPCConnectionError` with a clear message including the socket path.
Initialize `_conn_writer` and `_read_task`, set `_connected = True`.

**`send_command()` — correlation tracking with guaranteed cleanup:**

```python
async def send_command(self, name: str, payload: dict = {}, *,
                       timeout: float = DEFAULT_COMMAND_TIMEOUT,
                       raw_data: bytes | None = None) -> Envelope:
    if not self._connected:
        raise IPCSendError("Not connected")

    correlation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    envelope = create_envelope(
        from_module=self.module_name, to_module=self.target_module, type="command",
        name=name, payload=payload, correlation_id=correlation_id,
    )

    try:
        self._pending[correlation_id] = future
        await self._conn_writer.write_message(envelope, raw_data)
        response = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        raise IPCTimeoutError(
            f"Command '{name}' timed out after {timeout}s", correlation_id=correlation_id,
        )
    finally:
        self._pending.pop(correlation_id, None)

    if response.type == "error":
        raise IPCResponseError(
            response.payload.get("error", "Remote error"),
            correlation_id=correlation_id, error_payload=response.payload,
        )

    return response

```

**`send_event()` — fire and forget:**

```python
async def send_event(self, name: str, payload: dict = {}, raw_data: bytes | None = None) -> None:
    if not self._connected:
        raise IPCSendError("Not connected")
    envelope = create_envelope(from_module=self.module_name, to_module=self.target_module, type="event", name=name, payload=payload)
    await self._conn_writer.write_message(envelope, raw_data)

```

**`_read_loop()`:**

```python
async def _read_loop(self) -> None:
    try:
        while True:
            envelope, raw_data = await read_message(self._reader)

            if envelope.name == "ready":
                self._ready_event.set()
                continue

            if envelope.type in ("response", "error") and envelope.correlation_id:
                future = self._pending.get(envelope.correlation_id)
                if future is not None and not future.done():
                    future.set_result(envelope)
                continue

            # CRITICAL: FIFO ordering is guaranteed
            handlers = self._event_handlers.get(envelope.name, [])
            for handler in handlers:
                await handler(envelope, raw_data) # Inline await (no create_task)

    except (IPCDisconnectError, ConnectionResetError, asyncio.IncompleteReadError, IPCTimeoutError):
        pass
    except IPCProtocolError as exc:
        log.warning("Protocol error in read loop: %s", exc)
    finally:
        self._connected = False
        for future in self._pending.values():
            if not future.done():
                future.set_exception(IPCDisconnectError("Server disconnected"))
        self._pending.clear()
        self._ready_event.set()
        if self._on_disconnect_cb:
            try:
                await self._on_disconnect_cb()
            except Exception:
                pass

```

**`wait_ready()`:**

```python
async def wait_ready(self, timeout: float = 10.0) -> None:
    try:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise IPCTimeoutError(f"Server ready timeout after {timeout}s")
    if not self._connected:
        raise IPCDisconnectError("Disconnected before ready")

```

---

## 3. Protocol Framing

### JSON-only messages

```json
{"msg_id":"...","correlation_id":"","from":"loop","to":"tts","type":"command","name":"speak","timestamp":"...","payload":{"text":"Yes, sir."}}\n

```

### Binary payload messages

```json
{"msg_id":"...","from":"vad","to":"stt","type":"event","name":"audio_buffer","timestamp":"...","payload":{},"bytes":1920000}\n
<exactly 1920000 raw bytes>

```

### Defensive limits

* `StreamReader` limit: 1 MB.
* `bytes_length` cap: 10 MB. `validate_envelope` rejects anything larger.

---

## 4. Ready Protocol

The server sends a `ready` event unconditionally to every client upon connection.

**Rationale:** The `_ready` flag reflects the intent to serve and is set immediately before the socket is bound. Since `_handle_client` only runs when a client connects, this implies the server is already bound and listening. Therefore, the server simply sends `ready` unconditionally to every new connection. This ensures any client connecting late still receives the gate signal without race conditions.

---

## 5. Broadcast Protocol

Broadcast envelopes use `to_module = "*"`. Clients should accept messages where `to` is either their own module name or `"*"`.

---

## 6. Timeout Summary

| Timeout | Where | Mechanism | Value |
| --- | --- | --- | --- |
| Command response | `IPCClient.send_command()` | `asyncio.wait_for(future, timeout)` | 10 s |
| Connection attempt | `IPCClient.connect()` | `asyncio.wait_for(open_unix_connection(...))` | 2 s |
| Read assembly (Header) | `protocol.read_message()` | `asyncio.wait_for(readline, timeout)` | 30 s |
| Read assembly (Raw bytes) | `protocol.read_message()` | `asyncio.wait_for(readexactly, timeout)` | 3 s |
| Broadcast per-client write | `IPCServer.broadcast()` | `asyncio.wait_for(write_message, timeout)` | 100 ms |

---

## 7. Disconnect Propagation

Both client and server utilize sub-millisecond immediate EOF detection via Unix domain sockets kernel reporting.

---

## 8. Implementation Order

| Step | Files | Tests |
| --- | --- | --- |
| 1 | `errors.py`, `_constants.py` | Inheritance, attributes |
| 2 | `envelope.py` | Round-trip, validation, field mapping, UTC edge cases |
| 3 | `protocol.py` | JSON framing, binary framing, EOF detection, truncation, locking |
| 4 | `server.py` | Dispatch inline FIFO, ready emission, broadcast drop/evict modes |
| 5 | `client.py` | Connect/timeout, event FIFO, correlation tracking, `wait_ready` race-free |
| 6 | `__init__.py` | Import smoke test |
| 7 | `test_ping_pong.py` | All 12 integration acceptance criteria |
| 8 | `README.md` | API docs with examples |

---

## 9. Test Strategy

**Framework:** pytest + pytest-asyncio.
**Socket paths:** Tests use `/tmp/at_test_{uuid}/` to avoid needing root access.
**Integration Test:** Full coverage of Ping/Pong sequence, 100 concurrent commands, ready protocol, malformed JSON recovery, audio framing, and <100ms disconnection.

---

## 10. Resolution of Edge Case Uncertainties

The three behavioral uncertainties identified in earlier drafts have been natively resolved by the core user directives:

1. **Broadcast write timeout:** Handled via the strict `100 ms` timeout and safe `drop_on_backpressure` logging mode for real-time audio streams.
2. **Concurrent dispatch ordering:** Handled via enforcing sequential inline awaiting (`await handle()`). Full FIFO guarantee established.
3. **Read timeout on partial binary payload:** Reduced explicitly from 30 seconds to `3 seconds` to protect read assembly from indefinitely stalling on broken senders.
