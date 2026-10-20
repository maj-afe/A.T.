# at-ipc Implementation Plan

> [!IMPORTANT]
> This is the plan for review. **No code has been written.** Awaiting approval before proceeding.

---

## 1. File Structure

```
src/at_ipc/
├── __init__.py          # Public API re-exports
├── envelope.py          # Message envelope: dataclass, encode, decode, validation
├── protocol.py          # Wire protocol: framing, reading, writing (JSON + raw-byte)
├── server.py            # IPCServer: listen, accept, dispatch, ready, shutdown
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
│   └── test_ping_pong.py   # Two-service round-trip, the main acceptance test
└── conftest.py              # Shared fixtures (tmp socket paths, event loops)
```

**Rationale:** Five source modules, each under 300 lines, single responsibility. `envelope.py` and `protocol.py` are pure logic with no I/O — easy to unit test. `server.py` and `client.py` own all asyncio I/O. `errors.py` is tiny but avoids circular imports.

---

## 2. Module Breakdown

### 2.1 `errors.py` — Exception Hierarchy

```
IPCError (base)
├── ConnectionError       — socket connect failure, timeout
├── DisconnectError       — peer dropped unexpectedly
├── TimeoutError          — command response timeout (carries correlation_id)
├── ProtocolError         — malformed message, truncated payload
└── SendError             — write to closed/broken socket
```

- `TimeoutError` stores `correlation_id` as an attribute so the caller knows which request timed out.
- All inherit from a single `IPCError` base so callers can `except IPCError` broadly or narrowly.
- Named to not shadow builtins: `IPCConnectionError`, `IPCDisconnectError`, `IPCTimeoutError`, `IPCProtocolError`, `IPCSendError`.

### 2.2 `_constants.py` — Shared Constants

```python
SOCKET_DIR = Path("/run/at")
SOCKET_PERMISSIONS = 0o600
DEFAULT_COMMAND_TIMEOUT = 10.0      # seconds
CONNECTION_TIMEOUT = 2.0            # seconds
READ_TIMEOUT = 30.0                 # seconds
MAX_JSON_SIZE = 64 * 1024           # 64 KB, messages at or below this are pure JSON
MSG_TYPES = {"event", "command", "response", "error"}
```

### 2.3 `envelope.py` — Message Envelope

**Dataclass: `Envelope`**

| Field | Type | Default |
|---|---|---|
| `msg_id` | `str` | Auto-generated UUID4 |
| `correlation_id` | `str` | `""` |
| `from_module` | `str` | Required |
| `to_module` | `str` | Required |
| `type` | `str` | Required (validated against `MSG_TYPES`) |
| `name` | `str` | Required |
| `timestamp` | `str` | Auto-generated ISO 8601 UTC |
| `payload` | `dict` | `{}` |
| `bytes_length` | `int \| None` | `None` — present only for binary payloads |

**Functions:**

- `create_envelope(from_module, to_module, type, name, payload, correlation_id, bytes_length) → Envelope` — factory with defaults and validation.
- `envelope_to_dict(env: Envelope) → dict` — serialize to JSON-ready dict, mapping `from_module` → `"from"`, `to_module` → `"to"`, `bytes_length` → `"bytes"` (if set).
- `dict_to_envelope(d: dict) → Envelope` — deserialize, validate required fields, raise `IPCProtocolError` on missing/invalid.
- `validate_envelope(env: Envelope) → None` — checks `type` is valid, `msg_id` is set, timestamp is present.

**Design note:** `from` is a Python keyword — the dataclass field is `from_module`, serialized as `"from"` in JSON.

### 2.4 `protocol.py` — Wire Protocol

This module handles the framing layer: how bytes go on and off the wire. It operates on `asyncio.StreamReader` / `asyncio.StreamWriter` directly.

**Writing:**

```python
async def write_message(writer: StreamWriter, envelope: Envelope, raw_data: bytes | None = None) → None
```

1. If `raw_data` is provided (binary payload):
   - Set `envelope.bytes_length = len(raw_data)`
   - Serialize envelope to JSON, append `\n`, write.
   - Write `raw_data` (raw bytes, no encoding).
2. If `raw_data` is None (JSON-only):
   - Serialize envelope to JSON, append `\n`, write.
3. Call `writer.drain()` to respect backpressure.

**Reading:**

```python
async def read_message(reader: StreamReader) → tuple[Envelope, bytes | None]
```

1. Read one line (up to 1 MB limit to prevent DoS from malformed senders). Timeout: `READ_TIMEOUT`.
2. JSON-decode the line. If decode fails → `IPCProtocolError`.
3. Parse into `Envelope`.
4. If `envelope.bytes_length` is set and > 0:
   - `reader.readexactly(N)` with the same read timeout.
   - If `IncompleteReadError` → `IPCProtocolError` (truncated payload).
   - Return `(envelope, raw_data)`.
5. Else return `(envelope, None)`.

**Key detail — read timeout wrapping:** Every `reader.readline()` and `reader.readexactly()` is wrapped in `asyncio.wait_for(..., timeout=READ_TIMEOUT)`. On timeout → `IPCProtocolError`.

**Key detail — line size limit:** `reader.readline()` could block forever on a malicious sender that never sends `\n`. We set `reader._limit = 1_048_576` (1 MB) on the reader upon creation. asyncio's default is 2^16 which is fine for control messages, but we need headroom for large JSON headers that describe multi-MB audio. 1 MB is safe — a JSON envelope header is never more than a few KB.

### 2.5 `server.py` — `IPCServer`

```python
class IPCServer:
    def __init__(self, module_name: str, socket_path: Path | None = None)
```

**State:**
- `_module_name: str`
- `_socket_path: Path` — defaults to `/run/at/{module_name}.sock`
- `_handlers: dict[str, Callable]` — message name → async handler
- `_clients: dict[int, StreamWriter]` — fd → writer, for tracking connected clients
- `_server: asyncio.Server | None`
- `_on_disconnect: Callable | None` — app-level callback
- `_running: bool`

**Public API:**

| Method | Signature | Description |
|---|---|---|
| `on(name, handler)` | `(str, Callable[[Envelope, bytes\|None], Awaitable[dict\|None]]) → None` | Register handler for message name |
| `on_disconnect(callback)` | `(Callable[[str], Awaitable[None]]) → None` | Register disconnect callback (receives client info) |
| `start()` | `async () → None` | Create socket, start serving, emit `ready` |
| `stop()` | `async () → None` | Close all connections, remove socket file |

**Internal flow — `start()`:**

1. Ensure parent directory exists (but don't create `/run/at/` — systemd does that).
2. Remove stale socket file if it exists.
3. `asyncio.start_unix_server(self._handle_client, path=self._socket_path)`
4. `os.chmod(self._socket_path, 0o600)`
5. Set `_running = True`.
6. **Emit `ready`:** For every connected client… but wait — no clients are connected yet. The `ready` message is sent to each client *when they connect*. This is the correct interpretation: the server is *ready to accept commands* after emitting ready. So the server marks itself ready, and when a client connects, the first thing the server sends is the `ready` event. This way, a client connecting after startup still learns the server is ready.

   **Alternative interpretation:** The server broadcasts `ready` on its own socket, meaning clients that are already connected (or connect in the future) receive it. Since no clients exist at startup, the practical implementation is: set an internal `_ready = True` flag, and upon each new client connection, immediately send the `ready` event to that client.

**Internal flow — `_handle_client(reader, writer)`:**

1. Add writer to `_clients`.
2. If `_ready`, send `ready` event envelope to this client.
3. Enter read loop:
   ```
   while True:
       try:
           envelope, raw_data = await read_message(reader)
       except IPCProtocolError:
           log.warning("malformed message from client, ignoring")
           continue  # don't crash, don't drop
       except (ConnectionResetError, IPCDisconnectError, asyncio.IncompleteReadError):
           break  # client disconnected
       
       await self._dispatch(envelope, raw_data, writer)
   ```
4. On loop exit: remove from `_clients`, close writer, call `_on_disconnect` callback if set.

**Internal flow — `_dispatch(envelope, raw_data, writer)`:**

1. Look up `_handlers[envelope.name]`.
2. If no handler: log warning, if `envelope.type == "command"`, send error response.
3. If handler found: call `result = await handler(envelope, raw_data)`.
4. If `envelope.type == "command"` and handler returned a dict (not None): build a `response` envelope with `correlation_id = envelope.correlation_id`, serialize, write to `writer`.
5. If handler raises: catch, log, send error response if it was a command.

**Backpressure for slow clients:** `writer.drain()` is awaited after every write. If a client is slow, `drain()` will pause, which pauses that client's handling task but does **not** block other clients' tasks. Each client gets its own asyncio task via `start_unix_server`.

**SIGTERM handling:**

```python
loop = asyncio.get_running_loop()
loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.ensure_future(self.stop()))
```

`stop()` calls `_server.close()`, `await _server.wait_closed()`, closes all client writers, removes the socket file.

### 2.6 `client.py` — `IPCClient`

```python
class IPCClient:
    def __init__(self, module_name: str, target_module: str, socket_path: Path | None = None)
```

**State:**
- `_module_name: str` — who am I
- `_target_module: str` — who am I talking to
- `_socket_path: Path` — defaults to `/run/at/{target_module}.sock`
- `_reader: StreamReader | None`
- `_writer: StreamWriter | None`
- `_pending: dict[str, asyncio.Future]` — correlation_id → Future, for command/response matching
- `_event_handlers: dict[str, list[Callable]]` — event name → list of async callbacks
- `_on_disconnect: Callable | None`
- `_read_task: asyncio.Task | None` — the background reader loop
- `_connected: bool`

**Public API:**

| Method | Signature | Description |
|---|---|---|
| `connect()` | `async () → None` | Connect with 2s timeout, start read loop |
| `close()` | `async () → None` | Cancel read loop, close socket |
| `send_command(name, payload, timeout)` | `async (str, dict, float=10.0) → Envelope` | Send command, await correlated response |
| `send_event(name, payload)` | `async (str, dict) → None` | Fire and forget |
| `send_audio(name, payload_header, raw_data)` | `async (str, dict, bytes) → None` | Send binary payload |
| `on(name, callback)` | `(str, Callable) → None` | Subscribe to events by name |
| `on_disconnect(callback)` | `(Callable) → None` | Register disconnect callback |
| `wait_ready(timeout)` | `async (float=10.0) → None` | Block until server sends `ready` |

**Correlation ID tracking — `send_command()`:**

```python
async def send_command(self, name: str, payload: dict = {}, *, timeout: float = 10.0) -> Envelope:
    correlation_id = str(uuid.uuid4())
    future = asyncio.get_running_loop().create_future()
    self._pending[correlation_id] = future
    
    envelope = create_envelope(
        from_module=self._module_name,
        to_module=self._target_module,
        type="command",
        name=name,
        payload=payload,
        correlation_id=correlation_id,
    )
    await write_message(self._writer, envelope)
    
    try:
        response = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        del self._pending[correlation_id]
        raise IPCTimeoutError(f"Command '{name}' timed out after {timeout}s", correlation_id=correlation_id)
    
    return response
```

The Future is resolved by the background read loop when it receives a `response` or `error` with a matching `correlation_id`.

**Background read loop — `_read_loop()`:**

```python
async def _read_loop(self):
    try:
        while True:
            envelope, raw_data = await read_message(self._reader)
            
            if envelope.type in ("response", "error") and envelope.correlation_id:
                future = self._pending.pop(envelope.correlation_id, None)
                if future and not future.done():
                    future.set_result(envelope)
                elif future is None:
                    log.warning("Response with unknown correlation_id: %s", envelope.correlation_id)
            else:
                # Event or unsolicited message
                handlers = self._event_handlers.get(envelope.name, [])
                for handler in handlers:
                    asyncio.create_task(handler(envelope, raw_data))
    except (ConnectionResetError, asyncio.IncompleteReadError, IPCProtocolError):
        pass  # disconnected
    finally:
        self._connected = False
        # Fail all pending futures
        for cid, future in self._pending.items():
            if not future.done():
                future.set_exception(IPCDisconnectError("Server disconnected"))
        self._pending.clear()
        if self._on_disconnect:
            asyncio.create_task(self._on_disconnect())
```

**Disconnect detection:** When `read_message()` gets an EOF (empty read), or `ConnectionResetError`, the read loop exits. The `finally` block fires immediately — no polling. Detection latency is bounded by the time asyncio takes to surface the EOF, which is typically under 10 ms on a local Unix socket.

**`wait_ready()`:**

```python
async def wait_ready(self, timeout: float = 10.0) -> None:
    self._ready_event = asyncio.Event()
    # The read loop sets this event when it receives a 'ready' event
    await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
```

The read loop checks: if `envelope.name == "ready"` and we have a `_ready_event`, set it.

### 2.7 `__init__.py` — Public API

```python
from .server import IPCServer
from .client import IPCClient
from .envelope import Envelope, create_envelope
from .errors import (
    IPCError, IPCConnectionError, IPCDisconnectError,
    IPCTimeoutError, IPCProtocolError, IPCSendError,
)
```

Clean, minimal. Users do:
```python
from at_ipc import IPCServer, IPCClient
```

---

## 3. Protocol Framing Detail

### JSON-only messages (< 64 KB)

```
{"msg_id":"...","correlation_id":"","from":"audio","to":"loop","type":"event","name":"frame","timestamp":"...","payload":{...}}\n
```

One JSON line. Newline-terminated. Reader calls `readline()`.

### Binary payload messages (≥ 64 KB, or when raw_data is provided)

```
{"msg_id":"...","correlation_id":"","from":"vad","to":"stt","type":"event","name":"audio_buffer","timestamp":"...","payload":{},"bytes":2097152}\n
<exactly 2097152 raw bytes, no encoding, no newline>
```

Header is a JSON line (newline-terminated) containing `"bytes": N`. Reader calls `readline()` for the header, then `readexactly(N)` for the payload.

**Threshold decision:** The spec says "Messages under 64 KB use JSON only" and "Messages with payloads >64 KB use JSON-header-plus-raw-bytes format." The protocol layer doesn't enforce this as a hard boundary — it's driven by whether the caller passes `raw_data` to `write_message()`. If you pass raw_data, you get the binary format regardless of size. If you don't, you get JSON-only. This keeps the protocol layer simple and lets the application decide.

---

## 4. Timeout Implementation

| Timeout | Where | Mechanism |
|---|---|---|
| Command response (10s default) | `IPCClient.send_command()` | `asyncio.wait_for(future, timeout)` on the per-request Future |
| Connection attempt (2s) | `IPCClient.connect()` | `asyncio.wait_for(asyncio.open_unix_connection(...), timeout=2.0)` |
| Read assembly (30s) | `protocol.read_message()` | `asyncio.wait_for(reader.readline(), timeout=30.0)` and same for `readexactly()` |

All timeouts use `asyncio.wait_for()` — no timers, no polling, no threads.

---

## 5. Disconnect Propagation

### Server detects client disconnect

1. `read_message()` raises `ConnectionResetError` or returns EOF.
2. `_handle_client()` exits the read loop.
3. `finally` block: remove client from `_clients`, close writer, call `_on_disconnect(peer_info)`.
4. Latency: immediate — asyncio surfaces the EOF as soon as the kernel does.

### Client detects server disconnect

1. `read_message()` in `_read_loop()` raises `ConnectionResetError` or EOF.
2. Read loop exits.
3. `finally` block: set `_connected = False`, fail all pending futures with `IPCDisconnectError`, call `_on_disconnect()`.
4. Latency: immediate.

**Why < 100 ms:** Unix domain sockets are kernel-local. When one end closes (or the process dies), the kernel sends RST/EOF to the other end immediately. asyncio's event loop picks it up on the next iteration, which is sub-millisecond.

---

## 6. Implementation Order

Each step is a commit. Tests written alongside, not after.

| Step | Files | What | Tests |
|---|---|---|---|
| 1 | `errors.py`, `_constants.py` | Exception hierarchy, constants | `test_errors.py` (trivial) |
| 2 | `envelope.py` | Envelope dataclass, create/serialize/validate | `test_envelope.py` — round-trip, validation failures, field mapping |
| 3 | `protocol.py` | Wire framing: `write_message`, `read_message` | `test_protocol.py` — mock readers/writers, JSON framing, binary framing, truncation, malformed JSON |
| 4 | `server.py` | Full server implementation | `test_server.py` — start/stop, ready emission, handler dispatch, disconnect detection, malformed message resilience |
| 5 | `client.py` | Full client implementation | `test_client.py` — connect, send_command, send_event, correlation, timeout, disconnect detection |
| 6 | `__init__.py` | Re-exports | Import smoke test |
| 7 | `test_ping_pong.py` | Integration test: two services | Full round-trip, concurrency, ready protocol, audio payload, event broadcast, disconnect, malformed JSON |
| 8 | `README.md` | API documentation with examples | — |

---

## 7. Test Strategy

**Framework:** `pytest` with `pytest-asyncio` (or stdlib `unittest` with `asyncio`).

**Wait — constraint check.** The spec says "stdlib only." `pytest` is not stdlib. However, the spec says "pytest preferred" for tests and "no external dependencies" for the *library*. I interpret this as: the library has zero deps, the test suite may use pytest. If you disagree, I'll use `unittest` + `asyncio` exclusively.

**Socket paths in tests:** Tests use `/tmp/at_test_{uuid}/` to avoid needing `/run/at/` (which requires root). The `IPCServer` and `IPCClient` accept `socket_path` overrides for exactly this purpose.

**Test categories:**

1. **Unit tests** — `envelope.py` and `protocol.py` are tested with mock readers/writers. No real sockets.
2. **Server unit tests** — real Unix sockets in `/tmp`, single client, test dispatch/ready/disconnect.
3. **Client unit tests** — real Unix sockets in `/tmp`, test connect/send/receive/timeout/disconnect.
4. **Integration test** — `test_ping_pong.py`:
   - Ping server + Pong server, each with a client to the other.
   - Ping sends command "ping" to Pong, Pong responds with "pong".
   - 100 concurrent commands with unique correlation IDs.
   - 1000 sequential events, verify all received.
   - 2 MB audio payload, verify byte-for-byte integrity.
   - Kill server, verify client detects within 100 ms.
   - Kill client, verify server detects within 100 ms.
   - Send malformed JSON, verify server doesn't crash.
   - Command timeout test.
   - Ready protocol verification.

---

## 8. Open Questions

**Q1: Ready protocol — who receives it?**

My interpretation: The server sends a `ready` event to each client *upon connection*. There is no "broadcast to all existing clients" because at startup there are no clients. A client that connects later still needs to know the server is ready. This is the simplest correct behavior.

**Alternative:** The server could send `ready` once to the first client (the Loop Controller), and other clients don't get it. But this seems fragile and spec-inconsistent.

**I'm going with: every client gets `ready` upon connecting, if the server is already in the ready state.** Flag if this is wrong.

**Q2: `from` field and multiple clients.**

When the server sends a response, `from` = server's module name, `to` = the requesting client's module name (taken from the inbound command's `from` field). The server doesn't independently know who the client is until the client sends a message with a `from` field. This is fine — responses always reference the original command.

**Q3: The server does not broadcast events to all clients by default.**

The spec says "event broadcast" and "every subscriber receives every event." For the server, this means: when the *application* wants to broadcast an event, it calls a method that writes to all connected clients. The server itself doesn't spontaneously broadcast received events — that would be a message broker. I'll add:

```python
async def broadcast(self, name: str, payload: dict) → None
```

This writes the event to every connected client. Used for `ready` and any application-level broadcasts.

---

## 9. What I'm Least Confident About

1. **Backpressure under 100 concurrent clients.** `writer.drain()` should handle it, but I haven't verified asyncio's behavior when 100 Unix socket writers are all draining simultaneously. The acceptance test with 100 clients will validate this.

2. **Read timeout during binary payload.** If a sender sends the JSON header (with `bytes: 2000000`) then stops, `readexactly(2000000)` will block. Wrapping in `wait_for(30s)` handles it, but the partial read leaves the stream in an unrecoverable state. I'll close the connection on timeout, which is the correct behavior (the sender is broken).

3. **Signal handling in tests.** Sending SIGTERM to test server processes is fiddly. I'll use `server.stop()` directly in most tests and have one dedicated SIGTERM test.

---

*Ready for review. No code written yet.*
